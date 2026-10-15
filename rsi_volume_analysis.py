"""Alternate RSI + Volume analysis page.

This module intentionally reuses the existing RSI analysis API endpoints so the
original RSI analysis implementation remains unchanged.
"""

from flask import Blueprint, render_template

bp = Blueprint("rsi_volume_analysis", __name__)


@bp.route("/iifl/analysis-rsi-volume")
def page():
    return render_template("analysis_rsi_volume.html")
