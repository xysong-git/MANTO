"""Conservative Linux TCP occupancy guard, not an active-query metric.

An established foreign/unknown connection blocks an explicit benchmark even
when idle. Point-in-time checks cannot prove absence of short-lived clients
between observations; an operator-controlled exclusive window is still needed.
"""
import ipaddress
import os
from datetime import datetime

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.common import utc_now, ensure_utc


class ClientOccupancy(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    captured_at: datetime
    server_pid: int = Field(gt=0)
    port: int = Field(gt=0,le=65535)
    observer_pid: int = Field(gt=0)
    connection_count: int = Field(ge=0)
    foreign_pids: list[int]
    unknown_connections: int = Field(ge=0)
    passed: bool

    @field_validator('captured_at')
    @classmethod
    def utc(cls, value):return ensure_utc(value)


def _address(value):
    address=ipaddress.ip_address(value)
    if isinstance(address,ipaddress.IPv6Address):return address.ipv4_mapped or address
    return address


def inspect_clients(server_pid, port, *, own_pid=None, psutil_module=psutil):
    own_pid=os.getpid() if own_pid is None else own_pid
    connections=psutil_module.net_connections(kind='tcp')
    if not any(c.pid==server_pid and c.status==psutil_module.CONN_LISTEN
               and c.laddr.port==port for c in connections):
        raise ValueError('verified listener missing from occupancy inventory')
    clients={}
    for c in connections:
        if c.status==psutil_module.CONN_ESTABLISHED and c.raddr and c.raddr.port==port:
            clients[(_address(c.laddr.ip),c.laddr.port,_address(c.raddr.ip),c.raddr.port)]=c.pid
    foreign=set();unknown=0;count=0
    for c in connections:
        if c.pid!=server_pid or c.status!=psutil_module.CONN_ESTABLISHED or c.laddr.port!=port:
            continue
        count+=1
        if not c.raddr or not _address(c.raddr.ip).is_loopback:
            unknown+=1;continue
        pid=clients.get((_address(c.raddr.ip),c.raddr.port,_address(c.laddr.ip),c.laddr.port))
        if pid is None:unknown+=1
        elif pid!=own_pid:foreign.add(pid)
    return ClientOccupancy(captured_at=utc_now(),server_pid=server_pid,port=port,
        observer_pid=own_pid,connection_count=count,foreign_pids=sorted(foreign),
        unknown_connections=unknown,passed=not foreign and not unknown)
