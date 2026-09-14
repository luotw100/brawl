# 罗腾伟

185 0039 4109 | luotengwei@163.com | 北京 | 本科 · 西安理工大学 · 自动化（2014.09-2018.06）

目标岗位：自动驾驶 / 大模型 AI Infra —— 训练效率 · 编译器与推理引擎 · 训练框架基建

> 3.5年自动驾驶AI Infra，4.5年端侧部署与异构计算。

## 概要

### 我做过

**大模型训练效率**
- 自动驾驶VLA模型训练加速，**H卡MFU最高到45%左右**
- `Dataloader`/`DDP`/`FSDP`/`bf16`/`torch.compile`/`CUDA Graph`/`Activation Checkpoint`等技术的应用

**编译器与推理引擎**
- 自研编译器前端：**浮点→定点转换、图融合**，VLA/规控/机器人/座舱等模型落地
- 推理引擎：实现MLIR Interpreter，通过batch化、CUDA Graph、torch.compile、Triton Kernel、AoTI等手段，**最高加速60x以上**

**训练框架与基础设施**
- 训练框架：从Pytorch Lightning迁移到自研的xtorch，**支撑2000卡规模稳定训练**
- 监控系统：添加详细的关键节点埋点，可迅速诊断效率、崩溃问题
- 快速重启机制：多卡训练场景下热重载，**发生故障时分钟级别续训**
- 自动调优Agent：基于Lang Graph搭建自动性能调优系统，**AI可一键调优Infra相关超参**
- DT分离：将Dataloader和Training进程逻辑上分离，可将CPU密集的数据预处理分离到其它机器，**根治CPU竞争问题**
- 存储降本、视频解码、环境搭建、SGLang部署开源模型...

**多芯片适配与异构部署**
- 华为昇腾/阿里PPU：训推适配、精度对齐、性能调优
- 这些端侧平台的模型移植部署与性能优化：NVIDIA Jetson系列、海思Hi35xx系列、瑞芯微RK系列、高通/Cadense DSP、各类MCU/NPU...

### 我可以

- 从0到1搭建或重构训练框架与数据管线
- 端到端负责训推加速：瓶颈识别 → 针对性优化 → 规模验证
- 训练稳定性与成本治理：坏/慢节点识别、容错续训、GPU利用率与成本优化、存储治理
- 多芯片代码适配、性能调优、精度对齐

## 工作经历

### 小鹏汽车 · 自动驾驶 AI Infra 资深工程师 | 2024.10 - 至今 | 北京

**训练/推理效率**
- BEVFormer base的传统3D感知类模型，不同场景端到端提速1.7-2.4x，主要手段有：
    - 优化Dataloader流程
    - 按NUMA Node绑核
    - 解决跨进程数据传输瓶颈
    - 优化AllGather通信速度
    - 解决CPU OOM问题
    - Evaluation时数据传输瓶颈
- 2B大小的VLA模型，Sequence length从最初5K左右一路扩展到50K以上，H卡MFU达到45%左右，主要手段有：
    - DT分离的架构基本根治CPU竞争问题
    - 有计划的Python GC大大降低木桶效应
    - Fullgraph的torch.compile降低CPU Launch Kernel Bound
    - Checkpoint的精细控制及FSDP应用降低显存占用
    - ViT Packing提升ViT部分计算效率
    - Optimizer CPU offload，降低显存峰值压力
- Wan base的世界模型推理加速，端到端加速2x以上，主要手段有：
    - DiT部分int8量化，torch.compile，VAE的Decoder3D首帧缓存
    - Cross-Chunk的block cache(https://arxiv.org/abs/2604.20289)

**编译器与推理引擎**
- 定点化pass开发：将伪量化的IR计算图转为全定点的IR计算图，覆盖Matmul、LUT、加减乘除比较等类型算子
- 图融合：通过Concat传播思想将多个并行op合并为一个，提升计算效率
- 推理引擎开发及加速：实现IR的Interpreter，以及batch化、模拟int计算、triton op等加速手段
- 配套工具链开发：图变化正确性验证、推理性能评测、算子算力统计、AoTI导出等cli工具

**框架与基础设施**
- xtorch：自研训练框架，弃用Lightning，并作新框架的代码适配、精度对齐等工作，是下列优化的基础
- DT分离：将Dataloader分离到纯CPU的ECS上，自定义序列化/反序列化协议，通过socket直传数据，这样训练机器的CPU负载大幅降低。**此方案基本成为训练标配，一般来说至少有20%的端到端收益**
- 快速重启机制：将训练状态全量、短间隔的存到CPU内存，训练发生故障后可以尽量少的浪费训练步数以及快速恢复状态。**此容错方案在千卡级别训练时可以极大降低crash造成的算力浪费**
- 完善的监控体系：详细的关键位置埋点，可以在异常变慢、卡死、崩溃的时候，迅速定位原因
- TPilot：基于LangGraph，将上述监控数据喂给AI，让AI自动调优Infra相关参数，得到“免费”的性能提升

### Momenta · 高级软件工程师 · Infra | 2023.07 - 2024.10 | 北京

- fp16训练及auto scale的调优，加速60%以上
- 自动化AC：运行时perf各层级模型的显存/耗时开销，自动寻找“性价比”高的模块做activation checkpoint
- 昇腾 910B 适配：PETR base的检测模型适配、性能测试与精度对比
- 针对RepVGG结构模型的2:4稀疏训练，loss、评测指标可下降到和稠密训练一致，整网推理耗时下降13%左右
- 参考Deepspeed实现高性能的forward/backward transformer CUDA kernel，相比torch原版加速7%左右

### 字节跳动 · 机器学习算法引擎工程师 · AI-Lab | 2021.03 - 2023.07 | 北京

- 轻量化跨平台组件式算法引擎框架：内存25KB、纯C无依赖、新平台适配 1~2 天、支持Linux/Android/RTOS/裸系统、适配高通DSP/国芯DSP/纯CPU等平台、集成AEC/AGC/KWS等算法
- 支持端侧语音算法在大力台灯、自研工牌、火山引擎ToB等业务的落地

### 商汤科技 · 软件工程师 | 2018.03 - 2021.02 | 北京

- 开发安防ToB的算法SDK，包括检测、识别等各类CV类算法在端侧设备的落地
- 海思Hi35xx系列、NVIDIA Jetson系列、瑞芯微RK系列上的算法适配、精度对齐、性能优化
- 内部算法框架及组件开发，对算法屏蔽硬件差异，对用户屏蔽算法差异，开发并维护十多个算法组件

