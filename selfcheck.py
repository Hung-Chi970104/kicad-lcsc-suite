r"""Environment diagnostic for kicad-lcsc-suite.

Run with the *same* interpreter KiCad uses, so the answers reflect what the
plugin will actually see:

    macOS    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 selfcheck.py
    Windows  "C:\\Program Files\\KiCad\\10.0\\bin\\python.exe" selfcheck.py
    Linux    python3 selfcheck.py

Checks the interpreter version, the GUI toolkit, TLS trust, the vendored
converter, and live reachability of both storefronts. Exits non-zero if
something the plugin needs is missing.
"""

import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
LIB = os.path.join(HERE, "lib")
if os.path.isdir(LIB) and LIB not in sys.path:
    sys.path.insert(0, LIB)

MIN_PYTHON = (3, 9)

failures = []
warnings = []


def ok(label, detail=""):
    """Print a passing check."""
    print(f"  [ ok ] {label}" + (f" — {detail}" if detail else ""))


def warn(label, detail=""):
    """Print a non-fatal problem."""
    warnings.append(label)
    print(f"  [warn] {label}" + (f" — {detail}" if detail else ""))


def fail(label, detail=""):
    """Print a fatal problem."""
    failures.append(label)
    print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


print("kicad-lcsc-suite self-check")
print("=" * 60)

print("\nPlatform")
print(f"  {platform.platform()}")
print(f"  python  {sys.version.split()[0]}  ({sys.executable})")

print("\nInterpreter")
if sys.version_info >= MIN_PYTHON:
    ok(f"Python >= {'.'.join(map(str, MIN_PYTHON))}")
else:
    fail(
        f"Python >= {'.'.join(map(str, MIN_PYTHON))} required",
        f"found {sys.version_info.major}.{sys.version_info.minor}",
    )

print("\nGUI toolkit")
try:
    import wx

    ok("wxPython", wx.version())
    try:
        import wx.svg

        if hasattr(wx.svg, "SVGimage"):
            ok("wx.svg (symbol/footprint previews)")
        else:
            warn("wx.svg present but has no SVGimage", "previews disabled")
    except ImportError:
        warn("wx.svg missing", "previews disabled; everything else works")
    try:
        import wx.dataview  # noqa: F401

        ok("wx.dataview (result grids)")
    except ImportError:
        fail("wx.dataview missing", "the result list cannot be built")
except ImportError as exc:
    fail("wxPython missing", str(exc))

print("\nKiCad bindings")
try:
    import pcbnew

    version = getattr(pcbnew, "GetBuildVersion", lambda: "unknown")()
    ok("pcbnew importable", version)
except ImportError:
    warn(
        "pcbnew not importable",
        "expected outside KiCad; must work when run from KiCad's interpreter",
    )

print("\nTLS trust")
try:
    from lcsc.api import ssl_context

    context = ssl_context()
    try:
        import certifi

        ok("certifi CA bundle", certifi.where())
    except ImportError:
        warn("certifi missing", "falling back to the system CA store")
    ok("SSL context built", str(getattr(context, "verify_mode", "?")))
except Exception as exc:  # noqa: BLE001
    fail("could not build an SSL context", repr(exc))

print("\nVendored converter")
try:
    from easyeda2kicad.easyeda.easyeda_api import EasyedaApi  # noqa: F401
    from easyeda2kicad.kicad.export_kicad_symbol import (  # noqa: F401
        ExporterSymbolKicad,
    )

    ok("easyeda2kicad importable", LIB)
except ImportError as exc:
    fail("vendored easyeda2kicad not importable", str(exc))

print("\nPlugin modules")
for module in (
    "lcsc.api",
    "lcsc.importer",
    "lcsc.previewpanel",
):
    try:
        __import__(module)
        ok(module)
    except Exception as exc:  # noqa: BLE001
        fail(module, repr(exc))

print("\nLive endpoints (needs network)")
if "--offline" in sys.argv:
    print("  skipped (--offline)")
else:
    probe = "C374726"
    try:
        from lcsc import api

        assembly = api.jlc_assembly_detail(probe)
        if assembly:
            ok("JLC assembly detail", f"{probe} stock={assembly.get('stockCount')}")
        else:
            warn("JLC assembly detail returned nothing", "endpoint may have changed")

        retail = api.lcsc_retail_detail(probe)
        if retail:
            ok("LCSC retail detail", f"{probe} stock={retail.get('stockNumber')}")
        else:
            warn("LCSC retail detail returned nothing", "endpoint may have changed")

        total, hits = api.jlc_search("22k 0805", page_size=5)
        if hits:
            ok("JLC parts search", f"{total} total, {len(hits)} fetched")
            if any(h.attributes for h in hits):
                ok("parametric attributes present")
            else:
                warn("no parametric attributes", "filters will be empty")
        else:
            warn("JLC parts search returned nothing")
    except Exception as exc:  # noqa: BLE001
        warn("endpoint probe failed", repr(exc))

print("\n" + "=" * 60)
if failures:
    print(f"FAILED — {len(failures)} blocking problem(s): {', '.join(failures)}")
    sys.exit(1)
if warnings:
    print(f"OK with {len(warnings)} warning(s): {', '.join(warnings)}")
    sys.exit(0)
print("All checks passed.")
sys.exit(0)
