# NightReader · 夜读

轻量、离线的 Linux PDF 阅读器。支持夜间阅读、书签编辑、全文搜索、选字复制和高亮批注。

An offline Linux PDF reader with night modes, editable bookmarks, text search,
selection and highlights. Built with GTK 3 and PyMuPDF. The current interface is
primarily Chinese.

## 下载与安装

**首版安装包支持 Ubuntu 24.04 LTS、x86_64 / amd64。** 不提供 Windows、macOS 或 ARM 安装包；其他 Linux 发行版暂未验证，可以尝试下面的源码安装方式。

1. 打开 [Releases 下载页](https://github.com/zv201413/NightReader/releases/latest)。
2. 下载 `NightReader_0.1.0_ubuntu24.04_amd64.deb`。
3. 在下载目录打开终端执行：

```bash
sudo apt install ./NightReader_0.1.0_ubuntu24.04_amd64.deb
```

之后从应用菜单打开 **夜读 NightReader**，或右键 PDF → 打开方式 → NightReader。
安装会增加一种 PDF 打开方式，不改变你已有的默认阅读器。初次安装可能需要联网获取 Ubuntu 的 GTK/Python 组件；阅读和编辑过程不联网。

命令行：

```bash
nightreader book.pdf
nightreader first.pdf second.pdf
nightreader --settings
nightreader --version
```

下载页同时提供 `SHA256SUMS`，可用 `sha256sum -c SHA256SUMS --ignore-missing` 校验已下载文件。

卸载：

```bash
sudo apt remove nightreader
```

个人设置保留在 `${XDG_CONFIG_HOME:-~/.config}/nightread/`。

## 功能

- 原色、反相、暗灰柔化三种阅读模式；可调亮度、对比度和笔画。
- 连续滚动、逐页适宽，多本 PDF 各开一个独立窗口。
- 书签跳转和原地改名；新增、删除、调整层级。
- Ctrl+F 全文搜索，支持跨排版空白匹配；F3 / Shift+F3 切换结果。
- 连续选字和矩形选区、复制、高亮批注及颜色设置。
- 记住窗口尺寸、阅读位置和自定义快捷键。
- 舒适阅读与已有文字层校对视图。

**不包含 OCR。** 没有文字层的扫描 PDF 可以阅读，但不能直接搜索或复制文字。
文字层视图不会修正 PDF 中已有的识别错误。书签与批注需要手动保存；关闭有未保存修改的窗口时会提示。

| 快捷键 | 操作 |
| --- | --- |
| Ctrl+O | 打开 PDF |
| Ctrl+S | 保存书签和批注 |
| Ctrl+F | 搜索 |
| F3 / Shift+F3 | 下一条 / 上一条结果 |
| Esc | 关闭搜索 / 清除选区 |
| Ctrl+C | 复制选中文字 |
| Ctrl+H | 高亮选区 |
| Ctrl+Shift+R | 切换连续 / 矩形选字 |
| Ctrl+T | 切换原页 / 文字层 |
| Ctrl++ / Ctrl+- / Ctrl+0 | 放大 / 缩小 / 适宽 |
| D | 切换夜读模式 |
| Ctrl+, | 设置 |
| Ctrl+W | 关闭当前窗口 |

## 从源码运行

需要 Python 3.12 或更新版本、GTK 3、PyGObject 和 Cairo。Ubuntu 24.04：

```bash
sudo apt install git python3-venv python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0
git clone https://github.com/zv201413/NightReader.git
cd NightReader
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install .
.venv/bin/nightreader
```

GTK/PyGObject 由系统提供，虚拟环境必须使用 `--system-site-packages`。

## 开发、测试与打包

测试自动生成临时 PDF，不需要个人文件或私有样板；配置和写入均隔离到临时目录。

```bash
sudo apt install xvfb xauth xdotool dbus-x11
xvfb-run -a dbus-run-session --config-file=packaging/session-bus.conf -- .venv/bin/python packaging/run-tests.py
python3 packaging/build-deb.py
```

打包需在 **Ubuntu 24.04 amd64 / Python 3.12** 上执行，需要 `python3-pip` 和 `dpkg-deb`。
构建脚本按 `packaging/dependencies.json` 下载固定版本的 wheel 和对应源码，校验 SHA-256，然后生成 `.deb`、依赖源码归档和校验文件。应用运行时不调用 pip。

GitHub Actions 对代码执行测试、构建，并在干净 Ubuntu 容器中验证安装、GUI 搜索、保存和卸载。
推送 `v0.1.0` 这类版本标签后，只有所有检查通过才会发布 GitHub Release。

## 许可与反馈

使用 [GNU AGPL-3.0](LICENSE) 开源。依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

反馈问题请到 [Issues](https://github.com/zv201413/NightReader/issues)，附上系统版本、NightReader 版本和复现步骤。
涉及 PDF 时，可提供能够复现问题的最小示例。
