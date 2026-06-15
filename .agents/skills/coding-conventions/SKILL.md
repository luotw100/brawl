---
name: coding-conventions
description: 本项目编程规范，写/改任何代码之前必须先完整读过并照此执行——这是动手前的前置约束，不是事后查阅的参考。规范逐条扩充。当前收录：改动前先问用户要不要新建分支+worktree 的工作流；把"可选/合规"的判断收到边界、让核心逻辑零分叉（空对象模式消除"在不在"、边界前置校验消除"合不合法"）。
---

# 编程规范

> **动手前必读。** 写或改任何代码之前，先完整读过本篇并严格按这里的规范执行——这是前置约束，不是"遇到问题再翻"的参考手册。尤其当你准备写下 `if x is None`、`if not valid: raise` 这类分叉时，先回到本篇确认它该不该出现在这里。

本篇是一份会逐条扩充的规范集。每条规范都是中立、可复用的编程风格约定，与具体业务无关。当前收录：

---

## 工作流程：动手前先问"要不要开分支+worktree"

任何需要写/改代码的任务，**动手前先问用户：要不要为这次改动新建分支 + git worktree？**

- **用户答应** → 在 `.agent-worktrees/`（已在 `.gitignore` 里忽略）下新建一个 `分支名/` 子目录作为 worktree，并同时建好分支：
  ```bash
  git worktree add .agent-worktrees/<branch> -b <branch>
  ```
  之后所有改动、运行、验证都在这个 worktree 里进行，主工作树保持干净。**完成并验证后，再问用户：要不要把这个分支合入主分支？** 得到明确同意才合并，否则停在分支上交给用户处置。
- **用户拒绝** → 按用户的意思直接在当前工作树改。

这条和[铁律"未经授权不写代码"]是一对：先聊定方案拿授权，再问分支+worktree，最后才动手。

---

## 规范一：核心逻辑零分叉——把"可选/合规"的判断收到边界

一句话：**关于"可选"和"合规"的判断，全部收到边界（对象构造点 / 配置加载点）处理一次；核心逻辑拿到的东西默认"已存在、已合法"，不再重复判断。**

分叉（`if x is None`、`if not valid: raise`）散落在每个调用点，是坏味道——它把"边界上该回答一次的问题"泄露给了所有使用方，逼着每个使用方重复回答同一个问题。两条手法把它收回边界。

### 手法一：空对象模式（Null Object）——消除"在不在"

用 `None` 表达"这个可选依赖没给"，会逼着**每个调用点**都 `if x is not None` 一遍。改成：定义一个接口，给"没给"也写一个**满足同接口、方法全是空操作**的实现，由工厂在边界决定返回哪个。调用方从此无脑调用。

学名：**Null Object Pattern**（Bobby Woolf）。亲戚：**Replace Conditional with Polymorphism**（Fowler《重构》，用多态替换条件）、**Special Case Pattern**（Fowler PoEAA，Null 是它的一种）。底座是**依赖倒置（DIP）**：调用方依赖接口而非具体实现。

中立示例 —— 一个**可选**的"通知器"依赖：

```python
class Notifier(Protocol):
    def send(self, msg: str) -> None: ...

class EmailNotifier:
    def send(self, msg: str) -> None:
        ...  # 真发邮件

class NullNotifier:
    def send(self, msg: str) -> None:
        pass  # 没配通知渠道时用它，方法是空操作

def make_notifier(config) -> Notifier:   # 永不返回 None
    if config.notify_enabled:
        return EmailNotifier(config)
    return NullNotifier()
```

重构前，每个用到 notifier 的地方都得写 `if notifier is not None: notifier.send(...)`；改成永远拿到一个 `Notifier`（最差也是 `NullNotifier`），所有调用点的分叉一起消失。

idiom 提示：接口优先用结构化 `Protocol`，实现类**不必继承**、靠结构匹配即可。

### 手法二：边界前置校验——消除"合不合法"

**对参数的所有约束放在一个地方，给下游一个干净正确的环境。** 合法性检查绝不落在业务深处（跑到一半才 `raise`），要**前置到边界**——配置一加载就一次性查清，下游默认拿到的输入已合法，自己不再校验。这就是 **fail-fast at the boundary / "Parse, don't validate" / 让非法状态不可表示**，结果是**核心逻辑零校验分叉**。

校验该放哪，看它依赖什么数据 —— 分两类、两个落点：

- **字段内在 / 形状校验**（只看值本身是否良构：数量 > 0、长度匹配、字符串格式、枚举范围）→ 放进数据模型的**声明式 validator**（如 schema / pydantic validator），随配置解析一起完成。
- **跨引用 / 世界一致性校验**（值是否和**外部活数据**一致：引用的 id 是否已注册、文件路径是否真的存在、所需的外部依赖——环境变量 / 凭证 / 端口——是否就绪、是否和别处的数据对得上）→ 全部塞进**一个**显式函数 `validate(config, world)`，在真正开工**之前**调一次，违规抛一个统一的领域异常，由最外层入口（如 CLI）捕获并友好打印。典型反例：把"某凭证是否配好"留到运行期第一次真正用到它时才查，于是任务可能跑到一半才因缺凭证炸掉——这种检查要前移到边界。

为什么后一类**不**放进数据模型的 validator：它们依赖外部活数据（注册表、文件系统等），塞进去会让最底层的数据模型**反向依赖**这些外部资源，耦合方向就反了。独立的 `validate` 函数依赖"数据模型 + 外部资源"两者，不制造循环依赖。

**注意区分"校验"和"填默认"**：`validate` 只查合不合法、**不改值**；缺省补默认值是另一回事，归各自的 resolve / defaulting 步骤。两件事分开——校验过后，填默认是纯填充、零判断。（实践中两者常并肩放在同一个边界函数里，让它返回一份"解析好且已校验"的具体方案 `resolve_and_validate(config, world) -> plan`，下游拿现成的、不必重复解析。）

**边界保证生效后，删掉下游变冗余的守卫。** 一旦某不变量由边界统一保证（如"开工时每个被引用的模型都已配好凭证"），下游那些原本各自把关的检查就成了死代码——重复的 `if x is None: raise`、因枚举已被边界收窄而**不可达**的兜底分支等。把它们删掉，别当"防御性代码"留着；最多保留一个**会大声崩溃的纵深防御**（如直接下标 / 直接取键，违例即抛，而非静默兜底）。残留的死守卫会误导读者以为该非法态在这里仍可能发生。

中立示例：

```python
class AppError(Exception):
    ...  # 统一的领域异常，入口处捕获并友好打印（不甩 traceback）

def validate(config, registry) -> None:
    # 跨引用：引用的资源必须已注册
    if config.plugin_id not in registry:
        raise AppError(f"未知插件: {config.plugin_id}")
    # 世界一致性：输入目录必须真的存在
    if not config.input_dir.exists():
        raise AppError(f"输入目录不存在: {config.input_dir}")

def main():
    config = load_config()           # 字段 / 形状校验已在加载时完成
    try:
        validate(config, registry)   # 跨引用 / 一致性校验，开工前一次性查清
    except AppError as e:
        print(e)
        return
    run(config)                      # 下游零校验分叉
```

## 怎么用这条规范

写或改代码时，看到 `if x is None` / `if not valid` 这类分叉，先问：**这个判断能不能在边界回答一次，让这里不必判？** 能就用空对象或前置校验收回去。两者是同一个目标的两面——**核心逻辑只描述"做什么"，"在不在 / 合不合法"在边界就解决了。**
