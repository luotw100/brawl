# 工作流来源与采用范围

本工作流是在 2026-10-30 的调研和用户确认偏好基础上，为 brawl 独立编写的文字方法。没有安装下列项目，没有复制其执行脚本或导入其数据格式作为运行依赖。下列授权标识为调研时所见，直接引入其文件时应检查所用版本的许可证与署名要求。

| 来源 | 参考材料 | 采用的方法 | 本项目的调整 |
| --- | --- | --- | --- |
| autopunk-media-skills（MIT） | [场景拆解](https://github.com/ur-grue/autopunk-media-skills/blob/main/skills/tv-documentary/scripting/scene-breakdown-writer/SKILL.md)、[旁白写作](https://github.com/ur-grue/autopunk-media-skills/blob/main/skills/tv-documentary/scripting/commentary-writer/SKILL.md)、[粗剪评审](https://github.com/ur-grue/autopunk-media-skills/blob/main/skills/tv-documentary/post-production/rough-cut-notes/SKILL.md) | 明确段落功能、声音与画面配合、先修结构再修文字 | 把采访和实拍素材替换为玩家原话、局面和票型；不要求补拍或虚构反应 |
| Ripple（Apache-2.0） | [工作流](https://github.com/conmeara/ripple/blob/main/skills/ripple/SKILL.md)、[剪辑清单格式](https://github.com/conmeara/ripple/blob/main/schemas/edit.schema.json) | 区分长期偏好、当集视听脚本和素材选择理由，稳定定位后局部修改 | 以 Markdown 和游戏事件引用交付，音频生成前不伪造源媒体秒数 |
| agent-video-editor（MIT；媒体另有授权） | [多人访谈剪辑](https://github.com/kmilo93sd/agent-video-editor/blob/main/skills/editing-videos/recipes/podcast-to-youtube.md)、[评审清单](https://github.com/kmilo93sd/agent-video-editor/blob/main/skills/editing-videos/review-checklist.md) | 跟随对话与证据选择画面，剪短而保留原意 | 不照搬固定切镜频率、平台音量数值或全部导出检查，只保留影响文字脚本的判断 |
| StoryToolkitAI（GPL-3.0） | [Story Editor 说明](https://github.com/octimot/StoryToolkitAI#key-features)、[故事行与来源实现](https://github.com/octimot/StoryToolkitAI/blob/main/storytoolkitai/core/toolkit_ops/story.py) | 故事中的台词保留原始素材定位，可据此重排与核对 | 用源文件、事件号和精确选段定位；未引入该项目代码 |
| agentic-video-editing（MIT） | [脚本与镜头格式](https://github.com/real-simple-labs/agentic-video-editing/blob/main/skills/ve-video-edit-general/references/plan-format.md)、[应用审片意见](https://github.com/real-simple-labs/agentic-video-editing/blob/main/skills/apply-notes/SKILL.md) | 剧本行对应画面，修改意见映射回源脚本，区分本集修改与长期风格 | 使用段落/声音块 ID，去掉广告、macOS 和特定剪辑软件依赖，不照搬逐行切镜硬约束 |
| FireRed-OpenStoryline（Apache-2.0） | [中文脚本提示词](https://github.com/FireRedTeam/FireRed-OpenStoryline/blob/main/prompts/tasks/generate_script/zh/system.md) | 给每组素材提供叙事目的和文案预算 | 采用玩家原话为主的对局形式，不沿用第一人称 Vlog 口吻与默认情绪风格 |

这些来源提供方法与格式参考，不代表其效果已经在阿瓦隆对局上验证。本 skill 的专用约束是：生效版本选择、游戏规则与推论区分、逐步揭晓、公开声明与私密事实分离，以及保留真实比赛因果。
