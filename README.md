# AutoRent-autodl

**简体中文** | [English](docs/README.en.md)

在本地 Windows 运行的 AutoDL 自动租卡工具。选择地区、GPU 型号和数据盘扩容容量，自动等待余卡并完成租用。

![界面演示，数据为示例](docs/images/preview.png)

仓库首页、软件界面和命令行均默认中文。点击窗口顶部的“中文 | en”可即时切换界面语言，保留 Token、筛选条件与查询结果。英文文档通过页面的 English 链接查看，也可用 `start.cmd -Lang en` 直接启动英文界面；不会跟随系统语言自动切换。

## 快速开始

需要 Python 3.10+（含 tkinter），无需安装第三方依赖。

```powershell
git clone https://github.com/VinceTree/AutoRent-autodl.git
cd AutoRent-autodl
.\start.cmd
```

1. 登录 AutoDL，将网页 Token 粘贴到本地工具。获取步骤见[使用说明](docs/usage.md#打开与登录)。
2. 选择地区、GPU 型号和扩容数据盘容量；填 0 表示不扩容。
3. 点击“扫描当前余量”查看库存，或点击“开始自动租卡（计费）”持续等待并租用。

打开窗口不会自动下单。点击自动租卡后，每次只租一台单卡实例，成功后停止。

## 功能

- **按范围找卡**：地区、型号均可选择，没库存就等待，不擅自放宽范围。
- **数据盘扩容**：筛掉扩容余量不足的主机，自动将所选容量带入报价与创建。
- **自动读取信息**：账户当前价格、硬件配置和兼容镜像无需手填。
- **独立费用展示**：GPU 小时价与数据盘日费分开显示。
- **只读测试**：查询库存、核对报价，不创建实例；提交结果不明时停止重试。
- **中英文支持**：默认中文，英文界面与文档均为可选，不改变官网地区和型号标识。

## 常用入口

| 操作 | 入口 |
| --- | --- |
| 打开工具 | 双击根目录 `start.cmd` |
| 只读查询 | `start.cmd -ReadOnly` 或 `scripts/start-readonly.cmd` |
| 离线界面演示 | `start.cmd -Demo` |
| 英文界面 / 演示 | `start.cmd -Lang en` / `start.cmd -Lang en -Demo` |
| 运行测试 | `powershell -NoProfile -File scripts/test.ps1` |
| 命令行帮助 | `python -m autodl_quick --help` |
| 详细用法、配置与计费 | [使用说明](docs/usage.md) |

## 项目结构

```text
AutoRent-autodl/
├── autodl_quick/          # 应用源码
│   ├── core.py           # 库存、报价、扩容与租用流程
│   ├── catalog.py        # 地区与型号目录
│   ├── data/             # 随程序提供的公共目录快照
│   ├── locales/          # 英文翻译资源
│   └── ui/               # 主界面与高级偏好
├── config/               # 配置示例
├── docs/                 # 使用说明和界面图片
├── scripts/              # 启动、演示与测试脚本
├── tests/                # 离线与界面测试
├── README.md
└── start.cmd             # Windows 双击启动入口
```

本地 `config.json`、`.state/` 与 `.research/` 不提交到仓库。Token 只留在窗口内存；公共目录刷新缓存保存在 `.state/`，不会改动源码中的快照。详细约束和接口来源见[使用说明](docs/usage.md)。
