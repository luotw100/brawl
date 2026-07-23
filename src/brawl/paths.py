"""仓库根定位：从源码树运行（uv run）时，.env / web/ 等仓库级资源都相对这里解析。

包一旦被装进 site-packages 本锚点即失效——brawl 按源码项目运行，不走安装分发。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
