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
    # The command line sits on top of the stock models.
    modules = [p for p in DSP.glob("*.py") if p.name != "cli.py"]
    assert len(modules) > 10
    assert {p.name: stock_imports(p) for p in modules} == {p.name: [] for p in modules}


def test_the_check_sees_a_stock_import_at_any_depth(tmp_path):
    module = tmp_path / "module.py"
    module.write_text(
        "import fenykl.dsp.stock.copy\n"
        "from .stock import commands\n"
        "from . import stock\n"
        "def f():\n"
        "    from .stock.copy import ITEM\n"
        "from . import fields\n"
        "from .. import firmware\n"
    )
    assert stock_imports(module) == [1, 2, 3, 5]
