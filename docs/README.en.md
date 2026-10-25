# AutoRent-autodl

[简体中文](../README.md) | **English**

A local Windows tool for finding and renting AutoDL GPUs. Choose a region, GPU model and additional data disk capacity, then wait for matching stock and rent automatically.

![English interface — offline demo data](images/preview.en.png)

The application, CLI and repository homepage default to Chinese. English is opt-in through `-Lang en` or `--lang en`.

## Quick start

Requires Python 3.10+ with tkinter. No third-party dependencies are needed.

```powershell
git clone https://github.com/VinceTree/AutoRent-autodl.git
cd AutoRent-autodl
.\start.cmd -Lang en
```

1. Sign in to AutoDL and paste your web Token into the local tool. See [Token instructions](usage.en.md#launch-and-sign-in).
2. Choose the region, GPU model and additional data disk GB. Enter `0` for no expansion.
3. Click **Scan stock** to query, or **Start auto-rent (billed)** to wait and rent automatically.

Opening the app does not place an order. Auto-rent creates one single-GPU instance and stops after success.

## Features

- **Exact search scope:** select regions and models; waiting never broadens your selection.
- **Disk expansion:** exclude hosts with insufficient capacity and include the requested expansion in the order.
- **Automatic configuration:** load account prices, hardware information and compatible images.
- **Separate billing display:** show GPU prices per hour and data disk fees per day.
- **Read-only testing:** inspect stock and quotes without creating instances; stop on uncertain submissions.
- **English and Chinese:** both the main window and advanced preferences support either language.

Official region names and GPU identifiers remain as provided by AutoDL so filters and orders keep their exact meaning. The [English guide](usage.en.md#regions-and-models) includes a region name reference.

## Commands

| Action | Command |
| --- | --- |
| English interface | `start.cmd -Lang en` |
| Chinese interface | `start.cmd -Lang zh` or double-click `start.cmd` |
| Read-only English interface | `start.cmd -Lang en -ReadOnly` |
| Offline English demo | `start.cmd -Lang en -Demo` |
| Run tests | `powershell -NoProfile -File scripts/test.ps1` |
| English CLI help | `python -m autodl_quick --lang en --help` |
| Full usage and billing details | [English guide](usage.en.md) / [中文说明](usage.md) |

## Project structure

```text
AutoRent-autodl/
├── autodl_quick/          # Application source
│   ├── core.py           # Inventory, quotes, disk expansion and rental flow
│   ├── catalog.py        # Region and model catalog
│   ├── data/             # Bundled public catalog snapshot
│   ├── locales/          # English interface translations
│   └── ui/               # Main window and advanced preferences
├── config/               # Example configuration
├── docs/                 # English/Chinese guides and screenshots
├── scripts/              # Launch and test scripts
├── tests/                # Offline and GUI tests
├── README.md
└── start.cmd             # Windows launcher
```

Local `config.json`, `.state/` and `.research/` are excluded from Git. The Token stays in window memory. Refreshed catalogs are cached in `.state/` without changing the bundled snapshot. See the [guide](usage.en.md) for operating limits and API sources.
