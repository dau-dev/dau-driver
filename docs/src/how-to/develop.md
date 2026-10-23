# Develop and contribute

Contributions are welcome under the
[Apache 2.0 license](https://github.com/dau-dev/dau-driver/blob/main/LICENSE).
`kernel/linux-xdma/` is Xilinx's XDMA reference driver and keeps its own Dual
BSD/GPL terms. Every commit needs a Developer Certificate of Origin sign-off
(`git commit -s`), which a GitHub app checks on each pull request.

## Set up

```bash
git clone https://github.com/dau-dev/dau-driver.git
cd dau-driver
make develop
```

`make develop` installs the package in editable mode with its development
dependencies. The install builds a small native extension with nanobind, so it
needs a C++ toolchain. `make` with no arguments lists the targets.

Nothing here needs a board. The Python paths run against the in-memory fake
transport in `dau_driver.testing`, which is how the suite exercises them.

## Lint, type-check, test

```bash
make lint     # ruff for Python, clang-format for the extension under cpp/
make checks   # distribution check and ty
make test     # pytest
```

Run all three before opening a pull request. The Python blocks in the README
and the tutorials run as tests, and the tutorials' printed output is checked,
so an example that stops working fails the suite.

The hardware tests under `dau_driver/tests/integration/` skip unless they are
pointed at a board: `DAU_XDMA_MACOS=1` on a macOS host with the DriverKit
extension active, or `DAU_XDMA_HARDWARE=1` on a Linux XDMA host. Fence
any run that touches the bus with the deadman first; see
[how to arm the deadman](arm-the-deadman.md).

## Kernel drivers

The host drivers live under `kernel/`, each with its own build and load
instructions in its README. The Linux XDMA module is built against the running
kernel. The macOS DriverKit extension is built with Xcode; to check that it
compiles without signing it:

```bash
cd kernel/macos-xdma
make compile-unsigned
```

Whoever can open the character devices (Linux) or the user client (macOS) can
read and write the card's memory and registers. The operating system's device
permissions are the access control.

## Wheels

Release wheels are built with cibuildwheel in CI, from `[tool.cibuildwheel]` in
`pyproject.toml`. To build one locally:

```bash
make dist-py-wheel
```

## Documentation

The pages under `docs/src` and the README make up the documentation site. It
is built with [yardang](https://github.com/python-project-templates/yardang)
from `[tool.yardang]` in `pyproject.toml`, which lists the pages in order, and
the docs workflow publishes it to GitHub Pages after each green build on
`main`. To build it locally:

```bash
uv pip install "yardang[themes]"
yardang build
```

The site lands in `docs/html`.

## Pull requests

Work on a fork and open a pull request against `main`. A draft is fine while
the change is in progress. Squash into logical commits, all signed. Open an
issue first for anything larger than a fix, and use the discussions page for
questions.
