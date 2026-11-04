"""Agent package; preserves the original public import path."""
from .agent import PerformanceMonitoringAgent
from .agent import time  # compatibility for existing instrumentation

__all__ = ['PerformanceMonitoringAgent']
