import ast
from pathlib import Path

import fenykl.dsp

DSP = Path(fenykl.dsp.__file__).parent


def stock_imports(path: Path) -> list[int]:
    """The lines of `path` that import from fenykl.dsp.stock, at any depth."""
    lines = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                module = node.module or ""
            else:
                base = ["fenykl", "dsp"][: 3 - node.level]
                module = ".".join(base + ([node.module] if node.module else []))
            names = [module] + [f"{module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        if any(n.startswith("fenykl.dsp.stock") for n in names):
            lines.append(node.lineno)
    return lines


def test_the_codec_and_runtime_do_not_import_the_stock_models():
    modules = [p for p in DSP.glob("*.py") if p.name != "cli.py"]
    assert len(modules) > 10
    assert {p.name: stock_imports(p) for p in modules} == {p.name: [] for p in modules}


def test_the_check_sees_a_stock_import():
    assert stock_imports(DSP / "cli.py")
