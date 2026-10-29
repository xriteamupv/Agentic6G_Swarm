"""``config/network.yaml`` (SPEC A4.6-A4.7): the campus cell, the coverage map, link
profiles per level and the capacity model of the network stand-in.

Defaults keep the behaviour the test suite relies on (rule of SPEC A4.10): the network
grants every flow (``mode: always_grant``) and coverage comes from the campus shadow
polygons (``coverage.source: shadows``). Benches select the new modes explicitly.
"""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

LEVELS = ("good", "medium", "low", "lost")  # 2026-10-26: medium, low (were cell_edge, degraded)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AntennaConfig(_M):
    """One sector (SPEC A4.6.1): the 3GPP TR 38.901 Table 7.3-1 element pattern,
    A = -min(-(A_H + A_V), front_back_db), A_H = -min(12 (phi/h_bw)^2, front_back_db),
    A_V = -min(12 ((theta - tilt)/v_bw)^2, front_back_db)."""

    ports: int = 4
    gain_dbi: float = 10.0  # TODO(UPV)
    azimuth_deg: float | None = None  # bearing, degrees clockwise from north; None: omni
    tilt_deg: float | None = None  # electrical + mechanical downtilt
    h_beamwidth_deg: float = 65.0
    v_beamwidth_deg: float = 65.0
    front_back_db: float = 30.0
    #: the antenna stands this far back from the roof edge (the given site), towards the
    #: building: the roof hides the ground close to the building (NLOS) (TODO(UPV))
    setback_m: float = 5.0

    def pattern_db(self, bearing_deg: float, depression_deg: float) -> float:
        if self.azimuth_deg is None:
            return 0.0
        phi = (bearing_deg - self.azimuth_deg + 180.0) % 360.0 - 180.0
        a_h = -min(12.0 * (phi / self.h_beamwidth_deg) ** 2, self.front_back_db)
        theta = depression_deg - (self.tilt_deg or 0.0)
        a_v = -min(12.0 * (theta / self.v_beamwidth_deg) ** 2, self.front_back_db)
        return -min(-(a_h + a_v), self.front_back_db)


class CellConfig(_M):
    id: str
    site: str
    band: str = "n40"
    pci: int | None = None
    nci: int | None = None
    nrarfcn: int | None = None
    bandwidth_mhz: float = 20.0
    duplex: Literal["tdd", "fdd"] = "tdd"
    ul_slot_share: float = Field(0.3, gt=0.0, le=1.0)
    scs_khz: int = 30
    max_output_power_dbm: float = 26.5
    antenna: AntennaConfig = Field(default_factory=AntennaConfig)
    dl_mimo: str = ""

    @property
    def frequency_mhz(self) -> float:
        """NR-ARFCN to frequency (TS 38.104, 0-3 GHz raster: 5 kHz steps)."""
        if self.nrarfcn is None:
            return 2380.0
        return self.nrarfcn * 0.005

    @property
    def prb(self) -> int:
        """Transmission bandwidth in PRBs (TS 38.101-1 Table 5.3.2-1, FR1)."""
        table = {
            15: {5: 25, 10: 52, 15: 79, 20: 106, 25: 133, 30: 160, 40: 216, 50: 270},
            30: {5: 11, 10: 24, 15: 38, 20: 51, 25: 65, 30: 78, 40: 106, 50: 133},
        }
        return table.get(self.scs_khz, table[30]).get(int(self.bandwidth_mhz), 51)

    @property
    def rs_epre_dbm(self) -> float:
        """Reference-signal power per resource element, if the output power is the total
        over the carrier (TODO(UPV): total or per port)."""
        return self.max_output_power_dbm - 10.0 * math.log10(12 * self.prb)


class CalibrationTargets(_M):
    loop: str = "osm"
    rsrp_dbm: dict[str, float] = Field(
        default_factory=lambda: {"median": -86.0, "min": -95.0, "max": -77.0}
    )
    sinr_db: dict[str, float] = Field(default_factory=lambda: {"min": 8.0, "max": 26.0})


class SinrFromRsrp(_M):
    offset_db: float = 101.0
    clip_db: tuple[float, float] = (-10.0, 26.0)


class PathlossModel(_M):
    pathloss: Literal["3gpp_38901_uma"] = "3gpp_38901_uma"
    h_ut_m: float = 1.5
    los_from_footprints: bool = True
    shadow_fading: bool = False


class CoverageConfig(_M):
    source: Literal["shadows", "model", "radio_dt"] = "radio_dt"
    radio_dt_file: str | None = None
    model: PathlossModel = Field(default_factory=PathlossModel)
    calibration: str = "plots"
    calibration_targets: CalibrationTargets = Field(default_factory=CalibrationTargets)
    sinr_from_rsrp: SinrFromRsrp = Field(default_factory=SinrFromRsrp)
    sample_step_m: float = Field(2.0, gt=0.0)
    #: a grid over the whole pilot area, outdoors (0: footpaths only)
    grid_step_m: float = Field(4.0, ge=0.0)
    #: radio DT: each sample's RSRP becomes the median of the samples within this radius
    #: (0: off), so levels form consistent zones without single-cell islands (2026-10-26)
    smoothing_radius_m: float = Field(0.0, ge=0.0)
    #: rsrp (UPV, 2026-10-26: > -80 good, -100..-80 medium, -120..-100 low, < -120 lost)
    levels_by: Literal["sinr", "rsrp"] = "rsrp"
    thresholds_sinr_db: dict[str, float] = Field(
        default_factory=lambda: {"good": 13.0, "medium": 5.0, "low": -5.0}
    )
    thresholds_rsrp_dbm: dict[str, float] = Field(
        default_factory=lambda: {"good": -80.0, "medium": -100.0, "low": -120.0}
    )
    lost_rsrp_dbm: float = -120.0

    def edge_sinr(self, level: str) -> float:
        """The SINR at the lower edge of a level (from the RSRP threshold when levels are
        by RSRP: SINR derived as in ``sinr_from_rsrp``)."""
        if self.levels_by == "rsrp":
            rsrp = self.thresholds_rsrp_dbm.get(level, -120.0)
            lo, hi = self.sinr_from_rsrp.clip_db
            return min(max(rsrp + self.sinr_from_rsrp.offset_db, lo), hi)
        return self.thresholds_sinr_db.get(level, -5.0)

    hysteresis_db: float = 3.0
    #: foliage (campus ``vegetation_areas``): excess loss along the part of the ray inside
    #: the canopy (in leaf, ~2.4 GHz), capped (ITU-R P.833 order of magnitude)
    vegetation: dict[str, float] = Field(
        default_factory=lambda: {
            "loss_db_per_m": 0.4,
            "max_loss_db": 15.0,
            "canopy_height_m": 8.0,
            # a terminal under tree cover (ITU-R P.2108 clutter order of magnitude)
            "under_canopy_db": 8.0,
        }
    )
    min_dwell_s: float = 2.0
    shadows_override: bool = True
    #: the map file (``a6g network coverage --build`` / ``calibrate``), relative to config/
    map_file: str = "../sim_gazebo/src/a6g_gz_worlds/sim/{campus}_coverage_{source}.json"


class UplinkRate(_M):
    anchor: dict[str, float] = Field(default_factory=lambda: {"ul_mbps": 22.0, "sinr_db": 25.0})
    spectral_efficiency: dict[str, float] = Field(
        default_factory=lambda: {"alpha": 0.6, "se_max_bps_hz": 5.55}
    )

    def se(self, sinr_db: float) -> float:
        """Attenuated Shannon, capped at 64QAM: min(alpha log2(1 + SINR), se_max)."""
        a = self.spectral_efficiency["alpha"]
        cap = self.spectral_efficiency["se_max_bps_hz"]
        return min(a * math.log2(1.0 + 10.0 ** (sinr_db / 10.0)), cap)

    def rate_mbps(self, sinr_db: float) -> float:
        """Maximum uplink of one robot at a location: anchored at the measured 22 Mbps."""
        ref = self.se(self.anchor["sinr_db"])
        if ref <= 0:
            return 0.0
        return min(self.anchor["ul_mbps"] * self.se(sinr_db) / ref, self.anchor["ul_mbps"])


class LinkProfile(_M):
    ul_mbps: float | Literal["derived"] | None = None
    dl_mbps: float | None = None
    rtt_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None
    outage: bool = False


class FlowRequirement(_M):
    ul_mbps: float = 0.0
    always: bool = False
    best_effort: bool = False
    guaranteed: bool = False
    max_s: float | None = None
    max_rtt_ms: float | None = None


class CapacityConfig(_M):
    cells: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {"n40_nodo2_cell9": {"ul_capacity_mbps": 22.0}}
    )
    cost: Literal["spectral_efficiency"] = "spectral_efficiency"
    flow_requirements: dict[str, FlowRequirement] = Field(
        default_factory=lambda: {
            "telemetry": FlowRequirement(ul_mbps=0.0, always=True),
            "survey_uplink": FlowRequirement(ul_mbps=1.0, best_effort=True),
            "verification_burst": FlowRequirement(ul_mbps=8.0, max_s=30),
            "conversational": FlowRequirement(ul_mbps=0.1, max_rtt_ms=150),
            "video_control": FlowRequirement(ul_mbps=4.0, guaranteed=True),
            "telemetry_plus": FlowRequirement(ul_mbps=0.3, guaranteed=True),
        }
    )


class ReconcileConfig(_M):
    #: SPEC A4.7: off (experiment baseline), on (degradation loop), relocate (+ viewpoints),
    #: admission_only (baseline: the same admission as on, but a refused flow is not adapted)
    mode: Literal["off", "on", "relocate", "admission_only"] = "on"
    relocate_radius_m: float = 20.0
    recharge_reassign_min_priority: int = 80


class NetworkConfig(_M):
    mode: Literal["always_grant", "capacity"] = "always_grant"
    cells: list[CellConfig] = Field(default_factory=list)
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    uplink_rate: UplinkRate = Field(default_factory=UplinkRate)
    link_profiles: dict[str, LinkProfile] = Field(default_factory=dict)
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    reconcile: ReconcileConfig = Field(default_factory=ReconcileConfig)
    #: dB added to every SINR sample of the coverage map where the capacity stand-in reads it
    #: (admission rate and cost, viewpoints); 0 except in the SINR sensitivity runs
    sinr_shift_db: float = 0.0
    #: sensing mode of posts without one (SPEC A4.7 G13: video); None keeps telemetry_plus
    post_sensing_mode: Literal["video", "telemetry"] | None = None

    @field_validator("post_sensing_mode", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if v in ("", "none", "None") else v

    #: the noa_stub's model writes only nl.rationale; off in every bench (rule 21)
    stub_llm: bool = False
    #: where parameters come from (rule 32); copied into result manifests
    provenance: dict[str, str] = Field(default_factory=dict)

    def derived(self) -> dict[str, float]:
        """TS 38.306 maximum rates of the first cell, for the manifests (SPEC A4.6.1)."""
        if not self.cells:
            return {}
        c = self.cells[0]
        prb = c.prb
        mu_symbol_s = 14 * 1000 * (c.scs_khz / 15)

        def rate(layers: int, qm: int, overhead: float, share: float) -> float:
            return (
                1e-6 * layers * qm * (948 / 1024) * prb * 12 * mu_symbol_s * (1 - overhead) * share
            )

        return {
            "prb": prb,
            "ul_peak_1layer_64qam_mbps": round(rate(1, 6, 0.08, c.ul_slot_share), 1),
            "ul_peak_1layer_256qam_mbps": round(rate(1, 8, 0.08, c.ul_slot_share), 1),
            "dl_peak_4layer_256qam_mbps": round(rate(4, 8, 0.14, 1 - c.ul_slot_share), 1),
            "rs_epre_dbm": round(c.rs_epre_dbm, 1),
        }

    def level_profile(self, level: str) -> dict[str, float | None]:
        """Representative link values per level; ``derived`` uplink = the rate at the
        level's lower SINR edge (conservative)."""
        p = self.link_profiles.get(level) or LinkProfile()
        out: dict[str, float | None] = {
            "dl_mbps": p.dl_mbps,
            "rtt_ms": p.rtt_ms,
            "jitter_ms": p.jitter_ms,
            "loss_pct": p.loss_pct,
        }
        if p.outage or level == "lost":
            out["ul_mbps"] = 0.0
        elif p.ul_mbps == "derived" or p.ul_mbps is None:
            edge = self.coverage.edge_sinr(level)
            out["ul_mbps"] = round(self.uplink_rate.rate_mbps(edge), 2)
        else:
            out["ul_mbps"] = float(p.ul_mbps)
        return out
