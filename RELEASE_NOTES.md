第二个 Linux 公开版本。

**支持平台：Ubuntu 24.04 LTS，x86_64 / amd64。**

下载 `.deb` 文件后，在下载目录执行：

```bash
sudo apt install ./NightReader_0.2.0_ubuntu24.04_amd64.deb
```

从应用菜单打开「夜读 NightReader」，也可运行 `nightreader book.pdf`。

本版本新增：

- 设置面板新增「界面字体大小」调节（70%–200%），仅缩放界面字号、保留系统主题字体，不影响 PDF 正文。
- 工具栏由两行合并为一行，移除冗余的「上一页 / 下一页」按钮，超宽时横向滚动。

延续 0.1.0 的功能：

- 离线 PDF 阅读、反相与暗灰夜读模式、连续滚动和逐页适宽。
- 书签编辑、全文搜索、连续或矩形选字、复制和高亮批注。
- 多文档独立窗口、阅读位置与窗口尺寸记忆、自定义快捷键。

本版本界面主要为中文。不包含 OCR；无文字层的扫描 PDF 无法直接搜索。
其他 Linux 发行版、Windows、macOS、ARM 尚未提供经过验证的安装包。

`SHA256SUMS` 用于校验下载文件；`dependency-sources.tar.gz` 为随包依赖的对应源码，普通用户无需下载。
应用源代码见同版本标签及下方 GitHub Source code 下载。
