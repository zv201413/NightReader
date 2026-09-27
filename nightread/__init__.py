"""nightread —— 轻量 PDF 夜读 + 书签管理工具。

模块结构:
    app.py         Gtk.Application 生命周期、命令行参数
    window.py      主窗口:Paned(书签树 | 阅读区) + 工具栏 + 状态栏
    docworker.py   【核心】独占 Document 的单线程任务队列(K3)
    viewer.py      渲染视图 + K5 转换 + 坐标映射
    pixcache.py    字节预算 LRU 缓存(K8)
    config.py      状态持久化

设计红线(K3):UI 线程永远不持有 fitz.Document 对象,
一切文档操作经 docworker 投递。违反此条会导致段错误,不是可捕获的异常。
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
