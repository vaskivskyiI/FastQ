"""General-purpose plans: nd_scan (the dashboard's scan builder queues it), bluesky's count / scan
and the FastQ measurement building blocks, plus the queue flow (wait_until / wait_for / log_until /
sequence: unattended nights). Fixed-parameter versions of them (a temperature log,
a thermal cycle, ...) are presets in presets.yaml, not plans.
Plans imported from elsewhere are only offered when listed in PLANS.
"""
from bluesky.plans import count, scan  # list_scan: its *args cannot pass the queue-server validation

from fastq.plans.flow import log_until, sequence, wait_for, wait_until
from fastq.plans.measurements import aux_waveform, iv_curves, laser_sequence_scan, rt_sweep, set_temperature
from fastq.plans.scan import nd_scan

PLANS = [nd_scan, count, scan, aux_waveform, iv_curves, laser_sequence_scan, rt_sweep, set_temperature,
         wait_until, wait_for, log_until, sequence]
