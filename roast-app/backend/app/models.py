"""SQLAlchemy models: batches, *raw* samples, sourced events, calibration ledger.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A manual correction supersedes the previous row instead of deleting it, so
  every value keeps its ``source`` / ``created_by`` provenance.
* Calibrations live in a local ledger: every record carries channel, valid
  time range, formula + parameters, creator, version and current status.
  Records are never deleted or edited in place — the lifecycle
  draft -> active -> withdrawn | superseded is appended to
  ``calibration_history``.  A calibration only ever derives a *corrected view*
  at query time; raw samples, missing segments and events stay immutable.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DATABASE_URL


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    roaster: Mapped[str] = mapped_column(String(120), default="synthetic")
    bean: Mapped[str] = mapped_column(String(120), default="")
    charge_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    charge_temp_c: Mapped[float] = mapped_column(Float)
    ambient_temp_c: Mapped[float] = mapped_column(Float)
    target_drop_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )
    calibrations: Mapped[list["Calibration"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Calibration.id"
    )


class Sample(Base):
    """One raw probe reading.  Temperatures are NULL when the probe was
    briefly lost — the missing reading is preserved as missing, not invented."""

    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("batch_id", "t_s", name="uq_sample_batch_t"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Seconds since charge.  Intervals are intentionally uneven.
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bean_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    env_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # turning_point | first_crack_start | first_crack_end | drop |
    # damper_change | charge | custom
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    # auto = detected from the raw series; manual = operator entry.
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    # Numeric payload, e.g. new damper position (%) for damper_change.
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="events")


class Calibration(Base):
    """One calibration-ledger record: an auditable, versioned correction
    formula over a time range of ONE channel of ONE batch.

    Ledger rules (enforced here and in the API layer):

    * a record carries channel, valid range (seconds since charge), formula +
      parameters, creator, version and current status;
    * records are never deleted and never edited in place — lifecycle is
      ``draft -> active -> withdrawn | superseded`` and every transition is
      appended to :class:`CalibrationHistory`;
    * overlapping *active* records on the same channel may exist in the
      ledger, but analysis is blocked with a visible conflict until the
      operator explicitly adjudicates — nothing is silently picked;
    * applying a calibration only derives a corrected view at query time;
      raw samples are never rewritten.
    """

    __tablename__ = "calibrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # bean | env
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # Valid range in seconds since charge, inclusive on both ends.
    valid_from_s: Mapped[float] = mapped_column(Float, nullable=False)
    valid_to_s: Mapped[float] = mapped_column(Float, nullable=False)
    # Currently only "linear": corrected = gain * raw + offset.
    formula: Mapped[str] = mapped_column(String(40), default="linear")
    gain: Mapped[float] = mapped_column(Float, default=1.0)
    offset: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # draft | active | withdrawn | superseded
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    note: Mapped[str] = mapped_column(Text, default="")
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibrations.id"), nullable=True
    )
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("calibrations.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    batch: Mapped[Batch] = relationship(back_populates="calibrations")
    history: Mapped[list["CalibrationHistory"]] = relationship(
        back_populates="calibration",
        cascade="all, delete-orphan",
        order_by="CalibrationHistory.id",
    )


class CalibrationHistory(Base):
    """Append-only status-transition log for the calibration ledger."""

    __tablename__ = "calibration_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    calibration_id: Mapped[int] = mapped_column(
        ForeignKey("calibrations.id"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    acted_by: Mapped[str] = mapped_column(String(80), default="operator")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    calibration: Mapped[Calibration] = relationship(back_populates="history")


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
