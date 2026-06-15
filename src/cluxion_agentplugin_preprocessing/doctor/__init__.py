"""Embedded doctor for cluxion preprocessing plugin."""

from .framework import DoctorResult, render_json, render_text, run_doctor

__all__ = ["run_doctor", "render_text", "render_json", "DoctorResult"]
