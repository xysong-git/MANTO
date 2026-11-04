"""Opt-in host sampling for a verified local Linux IoTDB DataNode listener.

These are whole-host metrics, not per-process or IoTDB query metrics. A remote
endpoint, proxy, changed PID, or different namespace cannot use this source.
"""
from pathlib import Path
import ipaddress
import psutil
from pydantic import BaseModel, ConfigDict

from .system import PsutilSystemSampler
from ..schemas.monitoring import SystemSourceType, MetricUnavailableReason


class LocalDbIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    host: str
    port: int
    pid: int
    process_created_at_s: float
    main_class: str
    namespaces: dict[str, str]


def verify_local_db(host, port, pid):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError('local DB sampling requires a literal loopback endpoint')
    process = psutil.Process(pid)
    main = 'org.apache.iotdb.db.service.DataNode'
    if main not in process.cmdline():
        raise ValueError('listener is not an IoTDB DataNode')
    namespaces = {}
    for name in ('net', 'mnt', 'pid'):
        local = str((Path('/proc/self/ns')/name).readlink())
        remote = str((Path('/proc')/str(pid)/'ns'/name).readlink())
        if local != remote:
            raise ValueError('DB process namespace differs from sampling host')
        namespaces[name] = local
    address = ipaddress.ip_address(host)
    listeners = [c for c in process.net_connections(kind='tcp')
                 if c.status == psutil.CONN_LISTEN and c.laddr.port == port
                 and (ipaddress.ip_address(c.laddr.ip) == address
                      or c.laddr.ip == ('0.0.0.0' if address.version == 4 else '::')
                      or c.laddr.ip == '::')]
    if not listeners:
        raise ValueError('IoTDB PID does not own the configured listener')
    return LocalDbIdentity(host=host,port=port,pid=pid,
        process_created_at_s=process.create_time(),main_class=main,namespaces=namespaces)


class VerifiedLocalDbHostSampler(PsutilSystemSampler):
    """Own sampling state; never reuses or relabels a client snapshot."""
    _source_type = SystemSourceType.DB_HOST

    def __init__(self, *, host, port, pid, **kwargs):
        self.identity = verify_local_db(host, port, pid)
        super().__init__(**kwargs)

    def check_identity(self):
        now = verify_local_db(self.identity.host,self.identity.port,self.identity.pid)
        if now != self.identity:
            raise ValueError('DB process identity changed; new experiment required')

    def sample(self):
        self.check_identity()
        result = super().sample()
        self.check_identity()
        result.unavailable_fields['active_queries'] = MetricUnavailableReason.NO_DB_METRICS_SOURCE
        result.warnings.append('Verified local DataNode host; whole-host metrics include other processes; active queries UNKNOWN')
        return result
