# Vendored calculator notice

This directory contains the public WARDOGS artillery calculator frontend adapted for the RUBEZH panel. The upstream skin identifies `apollyon-sys/wardogs-calculator` as MIT-licensed. The project vendor should verify and retain the upstream MIT license and attribution if the frontend is redistributed publicly.

Adaptations in this panel:

- local `/calc` shell and same-origin asset proxy;
- local translations, icons, contours, and optional ballistic payloads;
- upstream telemetry, donation links, partner links, and mobile redirect disabled;
- project-neutral RUBEZH footer configuration.

The high-resolution tile pyramid is intentionally served through the fixed allowlist proxy in `app/main.py` rather than copied into this archive.
