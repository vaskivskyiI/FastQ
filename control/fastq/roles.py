"""What the plans expect from the device playing each lab.yaml role. Checked when a session
connects: a device that lacks attributes is still used, but the session says which plans
will fail with it. One PV naming per instrument class makes devices interchangeable:
temperature controllers Temp / Setpoint / Heater / Ramping / RampTarget / RampTime / RampStart /
RampStop; lock-ins X / Y / R / Theta / Frequency / Amplitude / TimeConstant / Phase / AuxOut<n>
(front-panel numbers); lasers ShutterOpen / PPEnable / Attenuator / PPDivider / BurstCount /
PulseDuration; motors Position / Moving / Velocity / Acceleration / Home / Stop.

    ROLE_INTERFACES[role] = {"needs": [attrs every plan of that role uses],
                             "optional": {attr: "what needs it"}}
"""
from __future__ import annotations

ROLE_INTERFACES = {
    "temperature": {"needs": ["temp", "setpoint"],
                    "optional": {"ramp_target": "sweep / rt_sweep / tm_rt_continuous (the ramp interface)",
                                 "ramp_time": "sweep / rt_sweep", "ramp_start_cmd": "sweep / rt_sweep",
                                 "ramp_stop_cmd": "sweep / rt_sweep"}},
    "lockin": {"needs": ["x", "y"],
               "optional": {"amplitude": "set_excitation / derived resistance", "aux_out1": "laser energy ramps"}},
    "laser": {"needs": ["shutter_open", "attenuator"],
              "optional": {"pp_enable": "pulses / ramp", "pp_divider": "pulses / ramp", "burst_count": "burst pulses",
                           "energy_control": "energy ramps (external energy)", "pulse_duration": "pulse duration steps",
                           "output_frequency": "burst timing"}},
    "keithley": {"needs": ["mode", "compliance", "acquire_cmd", "abort_cmd", "state", "mean", "n"],
                 "optional": {"readings": "I-V sweeps", "currents": "I-V sweeps", "last_reading": "continuous probe"}},
    "pulser": {"needs": ["shutter"], "optional": {}},
    "main_camera": {"needs": ["roi_list"], "optional": {}},
    "ref_camera": {"needs": ["roi_list"], "optional": {}},
}


def check(role: str, device) -> list[str]:
    """Problems of `device` in `role` (empty = fine)."""
    spec = ROLE_INTERFACES.get(role)
    if spec is None or device is None:
        return []
    out = []
    missing = [a for a in spec["needs"] if not hasattr(device, a)]
    if missing:
        out.append(f"role {role} -> {device.name}: missing {missing} - plans of this role will fail")
    opt = sorted({why for a, why in spec["optional"].items() if not hasattr(device, a)})
    if opt:
        out.append(f"role {role} -> {device.name}: cannot do {'; '.join(opt)}")
    return out
