# 第三方依赖说明

本项目源码采用 [MIT License](LICENSE)。Windows Release 中的 EXE 还会携带下表所列的运行时依赖；这些依赖继续按照各自的许可证发布，本文件不改变它们原有的版权和许可条件。

## 运行时依赖

版本以 [`requirements-lock.txt`](requirements-lock.txt) 为准。

| 依赖 | 版本 | 许可证 | 项目主页 / 许可证来源 |
| --- | ---: | --- | --- |
| beautifulsoup4 | 4.13.4 | MIT | [crummy.com/software/BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/bs4/) |
| certifi | 2025.8.3 | MPL-2.0 | [python-certifi](https://github.com/certifi/python-certifi) |
| charset-normalizer | 3.4.9 | MIT | [PyPI](https://pypi.org/project/charset-normalizer/) |
| idna | 3.18 | BSD-3-Clause | [PyPI](https://pypi.org/project/idna/) |
| lxml | 6.0.1 | BSD-3-Clause | [lxml.de](https://lxml.de/) |
| ping3 | 4.0.8 | MIT | [github.com/kyan001/ping3](https://github.com/kyan001/ping3) |
| Pillow | 12.3.0 | Pillow License（HPND） | [python-pillow.github.io](https://python-pillow.github.io/) |
| pystray | 0.19.5 | LGPL-3.0-or-later | [github.com/moses-palmer/pystray](https://github.com/moses-palmer/pystray) |
| requests | 2.32.5 | Apache-2.0 | [requests.readthedocs.io](https://requests.readthedocs.io/) |
| six | 1.17.0 | MIT | [github.com/benjaminp/six](https://github.com/benjaminp/six) |
| soupsieve | 2.8.4 | MIT | [github.com/facelessuser/soupsieve](https://github.com/facelessuser/soupsieve) |
| typing_extensions | 4.16.0 | PSF-2.0 | [PyPI](https://pypi.org/project/typing-extensions/) |
| urllib3 | 2.7.0 | MIT | [urllib3.readthedocs.io](https://urllib3.readthedocs.io/) |
| winrt-runtime 及 Windows.* 包 | 3.2.1 | MIT | [PyPI winrt-runtime](https://pypi.org/project/winrt-runtime/) |

依赖可能还会带入少量由上述项目声明的传递组件；其版本和来源以锁定文件及对应发行包的元数据为准。

## 构建工具

Release 使用 PyInstaller 生成。PyInstaller 本体采用 GPL-2.0-or-later，并附带允许分发生成程序的 bootloader exception；详情见 [PyInstaller 许可说明](https://pyinstaller.org/en/stable/license.html)。构建环境还使用 `requirements-build.txt` 中的 PyInstaller 版本及其公开依赖（例如 `pyinstaller-hooks-contrib`、`altgraph`、`pefile` 和 `pywin32-ctypes`），它们的许可仍由各自项目决定。

## 说明

- 本项目没有把第三方依赖的源码重新发布到仓库中；Release ZIP 只包含运行所需的打包文件和本项目文档。
- 如果你重新打包、修改或再分发本项目，请同时保留本文件、[`LICENSE`](LICENSE) 以及各依赖项目要求的版权和许可证声明。
