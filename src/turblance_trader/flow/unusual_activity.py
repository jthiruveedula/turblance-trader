"""Per-contract unusual-activity scoring for the turblance-trader flow engine.

Clean-room implementation: all methodology here is public-domain
options-market knowledge (session volume vs its own historical baseline,
robust z-scores via median absolute deviation). No proprietary data,
implementations, or branding are used or copied.

WHAT IT MEASURES (mechanical)
-----------------------------
For each option contract in a snapshot, the session's traded ``volume`` and
``open_interest`` are compared against a rolling-median baseline built from
that contract's own history — or, when contract history is thin, from a
strike/expiry bucket fallback. Deviations are expressed as robust z-scores:

    z = (x - median) / (1.4826 * MAD)

where MAD is the median absolute deviation. Per-strike unusual-activity
scores aggregate the call and put contract scores at the same strike (done
in ``profile.py``).

COLD START
----------
When there is no usable baseline for a contract — no history supplied at
all, or fewer than ``min_samples`` samples at both the contract level and
the bucket level — the contract is marked "insufficient baseline": every
score is NaN and ``has_baseline`` is False. Baselines are NEVER fabricated
from a single snapshot. A cold-start snapshot therefore yields an empty
``unusual_strikes`` list downstream; that is the honest answer, not an
error.

EVIDENCE STATUS
---------------
The z-scores and their aggregates are mechanical derivations of the input
data. Any *interpretation* of a high score as informed or urgent flow is
EXPERIMENTAL and UNVALIDATED until a backtest report says otherwise. Do
not trade on these outputs. Do not present them as reliable.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..capture.schema import SCHEMA_COLUMNS, validate_schema

_MAD_TO_SIGMA = 1.4826  # MAD -> sigma conversion for a normal distribution
_Z_CAP = 6.0  # cap on |z| when the baseline MAD is exactly zero

_CONTRACT_KEY_COLS = ("symbol", "expiry", "strike", "option_type")
_BUCKET_KEY_COLS = ("symbol", "expiry", "option_type")


def _contract_key(row):
    """Stable hashable identity for one option contract.

    Strike is rounded to 6 decimals so float repr noise cannot split one
    contract's history into two keys.
    """
    return (
        str(row["symbol"]),
        str(row["expiry"]),
        round(float(row["strike"]), 6),
        str(row["option_type"]),
    )


def _bucket_key(row):
    """Coarser fallback identity: (symbol, expiry, option_type)."""
    return (str(row["symbol"]), str(row["expiry"]), str(row["option_type"]))


def _robust_z(x, median, mad, z_cap=_Z_CAP):
    """Robust z-score of ``x`` against (median, MAD); NaN-safe.

    Returns NaN when ``x`` or the baseline is missing. When MAD is exactly
    zero (flat history), a value equal to the median scores 0.0 and any
    deviation scores a capped ``z_cap`` with the deviation's sign — a
    documented convention, not a statistical estimate.
    """
    if x is None or median is None or mad is None:
        return float("nan")
    try:
        x, median, mad = float(x), float(median), float(mad)
    except (TypeError, ValueError):
        return float("nan")
    if math.isnan(x) or math.isnan(median) or math.isnan(mad):
        return float("nan")
    if mad <= 0.0:
        if x == median:
            return 0.0
        return math.copysign(float(z_cap), x - median)
    return (x - median) / (_MAD_TO_SIGMA * mad)


class BaselineSet:
    """Rolling-median baselines for one snapshot's contracts.

    Built by :func:`compute_baselines` from concatenated historical
    snapshots. ``lookup`` prefers the per-contract baseline and falls back
    to the (symbol, expiry, option_type) bucket baseline when contract
    history is thin; both require at least ``min_samples`` samples.

    Attributes
    ----------
    min_samples : int
        Minimum samples for a baseline to count as usable.
    window : int
        How many most-recent samples per contract feed the rolling median.
    n_contracts : int
        Contracts with a usable per-contract baseline.
    n_buckets : int
        Buckets with a usable bucket baseline.
    """

    def __init__(self, contracts, buckets, min_samples=5, window=20):
        self._contracts = dict(contracts)
        self._buckets = dict(buckets)
        self.min_samples = int(min_samples)
        self.window = int(window)
        self.n_contracts = len(self._contracts)
        self.n_buckets = len(self._buckets)

    def lookup(self, key, bucket_key):
        """Return (volume_median, volume_mad, oi_median, oi_mad, source).

        ``source`` is "contract", "bucket", or None (insufficient
        baseline at both levels). Baselines are never invented: None is
        returned rather than a fabricated number.
        """
        hit = self._contracts.get(key)
        if hit is not None:
            return (*hit, "contract")
        hit = self._buckets.get(bucket_key)
        if hit is not None:
            return (*hit, "bucket")
        return (None, None, None, None, None)

    def __repr__(self):
        return (
            f"BaselineSet(contracts={self.n_contracts}, "
            f"buckets={self.n_buckets}, min_samples={self.min_samples}, "
            f"window={self.window})"
        )


def _baseline_stats(frame, min_samples):
    """Median/MAD stats for volume and OI from one group's recent samples.

    Returns a dict of stats or None when the group is too thin.
    """
    if len(frame) < min_samples:
        return None
    vol = pd.to_numeric(frame["volume"], errors="coerce").dropna()
    oi = pd.to_numeric(frame["open_interest"], errors="coerce").dropna()
    if len(vol) < min_samples or len(oi) < min_samples:
        return None
    vol_median = float(vol.median())
    oi_median = float(oi.median())
    vol_mad = float((vol - vol_median).abs().median())
    oi_mad = float((oi - oi_median).abs().median())
    return (vol_median, vol_mad, oi_median, oi_mad)


def compute_baselines(history_df, min_samples=5, window=20):
    """Build rolling-median baselines from concatenated history snapshots.

    Parameters
    ----------
    history_df : pandas.DataFrame
        One row per contract per historical snapshot, in the shared
        16-column contract schema (see ``capture/schema.py``). Must carry
        ``quote_time``; rows are ordered per contract by ``quote_time``
        and only the most recent ``window`` samples feed each median.
    min_samples : int, default 5
        Minimum samples for a baseline to count as usable, at both the
        contract and bucket levels. Thin histories degrade to the bucket
        baseline, then to "insufficient baseline" — never to a guess.
    window : int, default 20
        Rolling window: most-recent samples per contract used.

    Returns
    -------
    BaselineSet
        Per-contract and per-bucket (symbol, expiry, option_type)
        baselines with a ``lookup`` method. An empty history yields an
        empty BaselineSet (cold start), not an exception.
    """
    if history_df is None or len(history_df) == 0:
        return BaselineSet({}, {}, min_samples=min_samples, window=window)
    validate_schema(history_df)

    work = history_df.copy()
    work["_key"] = [_contract_key(r) for r in work.to_dict("records")]
    work["_bucket"] = [_bucket_key(r) for r in work.to_dict("records")]
    work["_qt"] = pd.to_datetime(work["quote_time"], utc=True, errors="coerce")
    work = work.sort_values("_qt", kind="stable")

    contracts = {}
    for key, group in work.groupby("_key", sort=False):
        recent = group.tail(int(window))
        stats = _baseline_stats(recent, min_samples)
        if stats is not None:
            contracts[key] = stats

    buckets = {}
    for key, group in work.groupby("_bucket", sort=False):
        # Bucket fallback pools across strikes: use the most recent
        # ``window`` *samples per contract*, not per bucket, so one
        # heavily-quoted contract cannot dominate the bucket median.
        per_contract = group.groupby("_key", sort=False).tail(int(window))
        stats = _baseline_stats(per_contract, min_samples)
        if stats is not None:
            buckets[key] = stats

    return BaselineSet(
        contracts, buckets, min_samples=min_samples, window=window
    )


def score_unusual_activity(df, prev_df=None, baselines=None, z_cap=_Z_CAP):
    """Score one snapshot's contracts for unusual volume/OI vs baselines.

    Parameters
    ----------
    df : pandas.DataFrame
        Current snapshot in the shared 16-column contract schema.
    prev_df : pandas.DataFrame, optional
        Previous snapshot (same schema) used for the ``oi_change``
        column (current OI minus previous OI, aligned on contract key).
        When None, ``oi_change`` is NaN — no fabrication.
    baselines : BaselineSet, optional
        From :func:`compute_baselines`. When None, every contract is
        "insufficient baseline" and all scores are NaN (cold start).
    z_cap : float, default 6.0
        Cap on |z| when a baseline's MAD is exactly zero.

    Returns
    -------
    pandas.DataFrame
        Copy of ``df`` with added columns:

        - ``volume_z``: robust z of session volume vs volume baseline.
        - ``oi_z``: robust z of open interest vs OI baseline.
        - ``oi_change``: OI minus previous-snapshot OI (NaN without prev).
        - ``contract_score``: max(volume_z, oi_z) clipped at 0; NaN when
          no baseline (cold start) — never invented.
        - ``has_baseline``: bool, False means "insufficient baseline".
        - ``baseline_source``: "contract" | "bucket" | None.
    """
    validate_schema(df)
    if prev_df is not None:
        validate_schema(prev_df)

    out = df.copy()
    keys = [_contract_key(r) for r in out.to_dict("records")]
    buckets = [_bucket_key(r) for r in out.to_dict("records")]

    vol = pd.to_numeric(out["volume"], errors="coerce").to_numpy(dtype=float)
    oi = pd.to_numeric(out["open_interest"], errors="coerce").to_numpy(
        dtype=float
    )

    # Previous-snapshot OI aligned on contract key.
    prev_oi = {}
    if prev_df is not None and len(prev_df):
        prev_oi = {
            _contract_key(r): r["open_interest"]
            for r in prev_df.to_dict("records")
        }

    volume_z, oi_z, oi_change = [], [], []
    contract_score, has_baseline, baseline_source = [], [], []
    for i, (key, bkey) in enumerate(zip(keys, buckets)):
        if baselines is not None:
            v_med, v_mad, o_med, o_mad, source = baselines.lookup(key, bkey)
        else:
            v_med = v_mad = o_med = o_mad = source = None

        vz = _robust_z(vol[i], v_med, v_mad, z_cap=z_cap)
        oz = _robust_z(oi[i], o_med, o_mad, z_cap=z_cap)
        volume_z.append(vz)
        oi_z.append(oz)

        prev = prev_oi.get(key)
        if prev is None or (isinstance(prev, float) and math.isnan(prev)):
            oi_change.append(float("nan"))
        elif math.isnan(oi[i]):
            oi_change.append(float("nan"))
        else:
            oi_change.append(float(oi[i]) - float(prev))

        ok = source is not None
        has_baseline.append(bool(ok))
        baseline_source.append(source)
        if not ok or (math.isnan(vz) and math.isnan(oz)):
            contract_score.append(float("nan"))
        else:
            best = max(
                0.0 if math.isnan(vz) else vz,
                0.0 if math.isnan(oz) else oz,
            )
            contract_score.append(float(best))

    out["volume_z"] = volume_z
    out["oi_z"] = oi_z
    out["oi_change"] = oi_change
    out["contract_score"] = contract_score
    out["has_baseline"] = has_baseline
    out["baseline_source"] = baseline_source
    return out
