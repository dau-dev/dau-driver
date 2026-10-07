## Prerequisites

Python 3.12 or later. On a host with a board, the Linux XDMA module from
`kernel/linux-xdma/` or the macOS DriverKit extension from `kernel/macos-xdma/`
must be loaded; without one, the package still imports and runs against its fake
transport.

## Install from the repository

There is no package index release yet. Install from a clone, or straight from
GitHub:

```bash
pip install git+https://github.com/dau-dev/dau-driver.git
```

For development, clone and install editable with the development extras:

```bash
git clone https://github.com/dau-dev/dau-driver.git
cd dau-driver
make develop
```

The optional native extension builds during the install when a C++ toolchain is
present; the package falls back to its Python transports when it is not.

## Kernel drivers

The host-side drivers live under [`kernel/`](https://github.com/dau-dev/dau-driver/tree/main/kernel)
with their own build and load instructions. Whoever can open the character
devices (Linux) or the user client (macOS) can read and write the card's memory
and registers; the operating system's device permissions are the access control.
