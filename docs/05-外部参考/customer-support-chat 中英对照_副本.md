# Claude 客服智能体实战指南（Customer Support Agent）

> 原文：[Customer support agent | Claude Docs](https://platform.claude.com/docs/en/about-claude/use-case-guides/customer-support-chat)
>
> 简介：用 Claude 构建一个客服聊天机器人——能回答产品问题、不跑题，还能通过工具调用（tool use）生成保险报价。

---

## 一、前置条件

* 一个 Claude API key（设置为 `ANTHROPIC_API_KEY` 环境变量）
* Python 3.10 或更高版本

安装所需的包：

```bash
pip install anthropic streamlit python-dotenv
```

---

## 二、动手之前：先想清楚三件事

### 2.1 判断是否适合用 Claude 做客服聊天

**适合引入 LLM 做客服自动化的信号：**

| 信号 | 说明 |
|------|------|
| 重复性问题量大 | Claude 擅长高效处理大量相似问题，把人工客服解放出来去处理更复杂的问题 |
| 需要快速整合信息 | Claude 能从庞大的知识库中快速检索、处理并组合信息；人工客服则要花时间查证多个来源 |
| 需要 7×24 全天候服务 | Claude 可全天候无疲劳工作；人工全天候轮班成本高、排班难 |
| 高峰期需快速扩容 | 咨询量暴涨时 Claude 可直接承接，无需临时招聘和培训 |
| 品牌口径需一致 | 可指示 Claude 始终如一地体现品牌语调和价值观；人工客服风格因人而异 |

**选 Claude 而非其他 LLM 的考量：**

1. **看重自然、细腻的对话**：Claude 的语言理解能力让对话更自然、更懂上下文，体验更像真人。
2. **常收到复杂、开放式提问**：Claude 能处理广泛话题，不靠"预设话术"，也无需把用户所有问法逐条编程穷举。
3. **需要可扩展的多语言支持**：Claude 支持 200 多种语言对话，无需为每种语言单独做机器人或大量翻译。

### 2.2 定义理想的聊天交互

先勾勒一次理想的客户交互过程，明确期望客户在什么情况下、以什么方式与 Claude 互动——这份大纲会帮你确定方案的技术需求。

**车险客服场景的理想交互示例：**

1. **客户发起聊天** → Claude 热情问候，主动开启对话
2. **客户询问新电动车的保险** → Claude 提供电动车保险的相关信息
3. **客户就电动车保险的特殊需求追问** → Claude 给出准确、有信息量的回答，并附来源链接
4. **客户问跑题问题（与保险/汽车无关）** → Claude 说明不讨论无关话题，把话题引导回车险
5. **客户表示想要报价** → Claude 依次做三件事：
   * 提出一组问题来确定合适的报价，并根据回答动态调整
   * 调用报价生成 API 工具，附上收集到的必要信息
   * 收到 API 返回结果后，组织成自然语言向客户展示报价
6. **客户继续追问** → Claude 回答追问，引导客户进入投保流程下一步，然后结束对话

> 💡 **提示**：为自己场景写示例时，建议把对话的实际话术逐句写出来——这样能顺便确定希望 Claude 具备的理想语气、回复长度和详细程度。

### 2.3 把交互拆解成独立任务

客服聊天看似一次完整对话，实际是多个任务的集合（问答、信息检索、执行操作等）。动手前先把理想交互拆解成每个具体任务，这样才能：

* 针对每个任务分别设计提示词和评估
* 清楚写测试用例时需要覆盖哪些交互情形

> 💡 **提示**：可以把拆解结果画成交互流程图，标出根据用户不同请求可能出现的对话转折点。

**保险示例拆出的关键任务：**

1. **问候与通用引导**
   * 热情问候客户，主动开启对话
   * 提供公司和本次服务的基本信息
2. **产品信息**
   * 提供电动车保险的信息（注意：这要求 Claude 的上下文中具备必要信息，可能需要接入 [RAG](https://platform.claude.com/cookbook/capabilities-retrieval-augmented-generation-guide)）
   * 回答电动车保险特殊需求相关的问题
   * 回答关于报价或保险条款细节的追问
   * 在合适的时候提供来源链接
3. **对话管理**
   * 不跑题（紧扣车险）
   * 把跑题的问题引导回相关话题
4. **报价生成**
   * 通过恰当的问题确认客户是否符合报价条件
   * 根据客户的回答动态调整问题
   * 把收集到的信息提交给报价生成 API
   * 向客户展示返回的报价

### 2.4 确立成功标准

与客服团队合作，[定义成功标准并编写详细的评估（evals）](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests)，给出可衡量的基准和目标。

**任务执行质量指标：**

| 指标 | 含义与测量方法 | 目标值 |
|------|----------------|--------|
| 意图理解准确率 | 抽查对话样本，评估 Claude 是否正确理解客户意图、关键下一步、"成功解决"的标准 | ≥ 95% |
| 回复相关性 | 给每条回复的相关性打分（可用 LLM 自动评分扩大规模） | ≥ 90% |
| 回复准确性 | 基于上下文信息，评估公司和产品基本信息是否正确 | 100% |
| 来源引用相关性 | 追踪提供链接/来源的频率和相关性 | 80% 的适用场景都给出相关来源 |
| 话题贴合度 | 评估回复是否紧扣车险或客户的具体问题 | ≥ 95% |
| 内容生成有效性 | 评估 Claude 判断"何时该生成报价"的能力及报价准确性 | 100%（关乎交互成败的关键信息） |
| 转人工效率 | 追踪"正确转人工"与"本该转人工却没转"的对话占比 | ≥ 95% |

**业务影响指标：**

| 指标 | 含义与测量方法 | 目标值 |
|------|----------------|--------|
| 情绪维持 | 用情感分析工具测量对话开头和结尾的客户情绪变化 | 90% 交互中情绪维持或改善 |
| 分流率（Deflection rate） | 无需人工介入、由机器人成功解决的咨询占比 | 70–80%（视咨询复杂度） |
| 客户满意度（CSAT） | 服务后问卷调查 | ≥ 4 分（满分 5 分） |
| 平均处理时长（AHT） | 机器人解决一次咨询的平均耗时 | 低于人工客服 |

---

## 三、实现步骤

### 3.1 选择合适的模型

模型选型取决于**成本、准确性、响应速度**之间的权衡：

* **Claude Opus 5**：平衡智能、延迟和成本，适合大多数客服场景，包括需要在长多轮对话中深度推理的复杂场景。
* **Claude Haiku 4.5**：如果对话流程包含多次提示词调用（RAG、工具调用）或长上下文提示词，选它更能优化延迟。

### 3.2 构建强提示词

用 Claude 做客服，需要给它足够的指引和上下文以恰当回应，同时保留足够灵活性来应对各式提问。

**第一步：写身份设定（system prompt）。** 创建 `config.py` 文件：

```python
IDENTITY = """You are Eva, a friendly and knowledgeable AI assistant for Acme Insurance
Company. Your role is to warmly welcome customers and provide information on
Acme's insurance offerings, which include car insurance and electric car
insurance. You can also help customers get quotes for their insurance needs."""
```

> 💡 **提示**：你可能会想把所有信息都塞进 system prompt 以便与用户对话分开。但实际上 Claude 在把提示词主体内容放在第一轮 `User` 消息里时效果最好（唯一例外是角色设定）。详见[用 system prompt 为 Claude 设定角色](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices#give-claude-a-role)。

**第二步：分块编写提示词主体。** 把复杂提示词拆成小块逐块编写，便于调试时快速单独调整。先写公司介绍等静态上下文：

```python
STATIC_GREETINGS_AND_GENERAL = """
<static_context>
Acme Auto Insurance: Your Trusted Companion on the Road

About:
At Acme Insurance, we understand that your vehicle is more than just a mode of transportation—it's your ticket to life's adventures.
Since 1985, we've been crafting auto insurance policies that give drivers the confidence to explore, commute, and travel with peace of mind.
Whether you're navigating city streets or embarking on cross-country road trips, Acme is there to protect you and your vehicle.
Our innovative auto insurance policies are designed to adapt to your unique needs, covering everything from fender benders to major collisions.
With Acme's award-winning customer service and swift claim resolution, you can focus on the joy of driving while we handle the rest.
We're not just an insurance provider—we're your co-pilot in life's journeys.
Choose Acme Auto Insurance and experience the assurance that comes with superior coverage and genuine care. Because at Acme, we don't just
insure your car—we fuel your adventures on the open road.

Note: We also offer specialized coverage for electric vehicles, ensuring that drivers of all car types can benefit from our protection.

Acme Insurance offers the following products:
- Car insurance
- Electric car insurance
- Two-wheeler insurance

Business hours: Monday-Friday, 9 AM - 5 PM EST
Customer service number: 1-800-123-4567
</static_context>
"""
```

再写车险和电动车险的产品信息：

```python
STATIC_CAR_INSURANCE = """
<static_context>
Car Insurance Coverage:
Acme's car insurance policies typically cover:
1. Liability coverage: Pays for bodily injury and property damage you cause to others.
2. Collision coverage: Pays for damage to your car in an accident.
3. Comprehensive coverage: Pays for damage to your car from non-collision incidents.
4. Medical payments coverage: Pays for medical expenses after an accident.
5. Uninsured/underinsured motorist coverage: Protects you if you're hit by a driver with insufficient insurance.

Optional coverages include:
- Rental car reimbursement
- Roadside assistance
- New car replacement
</static_context>
"""

STATIC_ELECTRIC_CAR_INSURANCE = """
<static_context>
Electric Car Insurance:
Our specialized electric car insurance goes beyond traditional auto coverage,
offering tailored protection for your high-tech investment. In addition to standard coverages, we offer:
- Safeguarding expensive battery systems
- Covering home charging equipment
- Adapting to the rapidly evolving world of electric vehicles

Whether you're driving a Rivian, Nissan Leaf, Chevrolet Bolt,
or any other electric vehicle, our knowledgeable agents are ready to craft a policy
that gives you the confidence to enjoy every electron-powered mile.
</static_context>
"""
```

**第三步：添加示例对话。** 添加至少 4–5 个"好的"示例对话引导 Claude 的回复，应能代表理想的客户交互，可包含护栏规则和工具调用等元素：

```python
EXAMPLES = """
Here are a few examples of how you can interact with customers:

<example 1>
H: Hi, do you offer commercial insurance for small businesses?

A: Ah! Unfortunately, we don't offer commercial insurance at this time.
However, we do provide a range of personal insurance products including car insurance and electric car insurance.
Would you like to know more about these offerings?
</example 1>

<example 2>
H: Hi, what are your business hours?

A: Our business hours are Monday to Friday, 9 AM to 5 PM EST.
If you have any questions or need assistance, feel free to reach out during those times.
How else can I assist you today?
</example 2>

<example 3>
H: I'm shopping for car insurance. What does Acme offer?

A: Great choice! Acme's car insurance policies typically include:
- Liability coverage: Protects you if you're at fault in an accident
- Collision coverage: Pays for damage to your car in an accident
- Comprehensive coverage: Covers non-collision incidents like theft or vandalism
- Medical payments coverage: Pays for medical expenses after an accident
- Uninsured/underinsured motorist coverage: Protects you from drivers with insufficient insurance

We also offer optional coverages like rental car reimbursement, roadside assistance, and new car replacement.
Would you like more details on any of these coverages, or do you have specific questions about insuring your vehicle?
</example 3>

<example 4>
H: I'd like to hear about your electric car insurance.

A: Certainly! Our electric car insurance is specially designed to meet the unique needs of electric vehicle owners.
In addition to all the standard coverages we offer for regular cars, our electric car policies include:

- Battery coverage: Protection against damage or degradation of your vehicle's expensive battery system
- Tech package: Covers repairs or replacement of advanced electronics and onboard computer systems
- Charging station coverage: Protects both home and portable charging equipment
- Cybersecurity protection: Coverage against hacking or digital threats
- Specialized roadside assistance: Includes flatbed towing to protect the vehicle's undercarriage

We cover a wide range of electric vehicles, including popular models from Tesla, Rivian, Nissan, Chevrolet, and more.
Is there a specific electric vehicle you're looking to insure, or would you like more details on any of these coverages?
</example 4>

<example 5>
H: I'd like to get a quote for my car insurance.

A: Certainly! I'd be happy to help you get a quote for your car insurance.
To provide you with an accurate quote, I'll need to collect some information about your vehicle and the primary driver.
Let's start with the basics:

1. What is the make and model of your vehicle?
2. What year was it manufactured?
3. Approximately how many miles have you driven?
4. What is the age of the primary driver?

Once you provide this information, I'll use our quoting tool to generate a personalized insurance quote for you.
</example 5>
"""
```

**第四步：添加行为护栏。** 明确 Claude 与客户互动时"该做什么、不该做什么"，内容可来自品牌规范或客服政策：

```python
ADDITIONAL_GUARDRAILS = """Please adhere to the following guardrails:
1. Only provide information about insurance types listed in our offerings.
2. If asked about an insurance type we don't offer, politely state
that we don't provide that service.
3. Do not speculate about future product offerings or company plans.
4. Don't make promises or enter into agreements it's not authorized to make.
You only provide information and guidance.
5. Do not mention any competitor's products or services.
"""
```

**第五步：合并为完整提示词。**

```python
TASK_SPECIFIC_INSTRUCTIONS = " ".join(
    [
        STATIC_GREETINGS_AND_GENERAL,
        STATIC_CAR_INSURANCE,
        STATIC_ELECTRIC_CAR_INSURANCE,
        EXAMPLES,
        ADDITIONAL_GUARDRAILS,
    ]
)
```

### 3.3 用工具调用增加动态与智能体能力

通过客户端工具调用（tool use），Claude 可以动态执行操作和检索信息。先列出需要用到的外部工具或 API，本例从一个计算报价的工具开始。

> 💡 **提示**：这个"工具"本身不会真正执行计算——它只是让模型向应用程序发出信号：应该使用某工具并附上指定的参数。

把模型名称、工具定义和桩（stub）实现加入 `config.py`：

```python
import time

MODEL = "claude-opus-5-5"

TOOLS = [
    {
        "name": "get_quote",
        "description": "Calculate the insurance quote based on user input. Returned value is per month premium.",
        "input_schema": {
            "type": "object",
            "properties": {
                "make": {"type": "string", "description": "The make of the vehicle."},
                "model": {"type": "string", "description": "The model of the vehicle."},
                "year": {
                    "type": "integer",
                    "description": "The year the vehicle was manufactured.",
                },
                "mileage": {
                    "type": "integer",
                    "description": "The mileage on the vehicle.",
                },
                "driver_age": {
                    "type": "integer",
                    "description": "The age of the primary driver.",
                },
            },
            "required": ["make", "model", "year", "mileage", "driver_age"],
        },
    }
]


def get_quote(make, model, year, mileage, driver_age):
    """Returns the premium per month in USD"""
    # You can call an http endpoint or a database to get the quote.
    # Here, we simulate a delay of 1 seconds and return a fixed quote of 100.
    time.sleep(1)
    return 100
```

### 3.4 部署提示词（ChatBot 类）

不把提示词部署到类生产环境[跑评估](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests)，就很难知道它好不好用。下面用提示词 + Anthropic SDK + Streamlit 构建一个带界面的小应用。

在 `chatbot.py` 中创建 ChatBot 类，封装与 Anthropic SDK 的交互。两个核心方法：`generate_message`（调用 API 生成消息）和 `process_user_input`（处理每条用户输入）：

```python
# In your chatbot.py, import these from the config.py you wrote above:
# from config import IDENTITY, TOOLS, MODEL, get_quote
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()


class ChatBot:
    def __init__(self, session_state):
        self.anthropic = Anthropic()
        self.session_state = session_state

    def generate_message(
        self,
        messages,
        max_tokens,
    ):
        try:
            response = self.anthropic.messages.create(
                model=MODEL,
                system=IDENTITY,
                max_tokens=max_tokens,
                messages=messages,
                tools=TOOLS,
            )
            return response
        except Exception as e:
            return {"error": str(e)}

    def process_user_input(self, user_input):
        self.session_state.messages.append({"role": "user", "content": user_input})

        response_message = self.generate_message(
            messages=self.session_state.messages,
            max_tokens=2048,
        )

        if "error" in response_message:
            return f"An error occurred: {response_message['error']}"

        if response_message.content[-1].type == "tool_use":
            tool_use = response_message.content[-1]
            func_name = tool_use.name
            func_params = tool_use.input
            tool_use_id = tool_use.id

            result = self.handle_tool_use(func_name, func_params)
            self.session_state.messages.append(
                {"role": "assistant", "content": response_message.content}
            )
            self.session_state.messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": f"{result}",
                        }
                    ],
                }
            )

            follow_up_response = self.generate_message(
                messages=self.session_state.messages,
                max_tokens=2048,
            )

            if "error" in follow_up_response:
                return f"An error occurred: {follow_up_response['error']}"

            response_text = next(
                (block.text for block in follow_up_response.content if block.type == "text"),
                None,
            )
            if response_text is None:
                raise Exception("An error occurred: Unexpected response type")
            self.session_state.messages.append(
                {"role": "assistant", "content": response_text}
            )
            return response_text

        text_block = next(
            (block for block in response_message.content if block.type == "text"), None
        )
        if text_block is not None:
            response_text = text_block.text
            self.session_state.messages.append(
                {"role": "assistant", "content": response_text}
            )
            return response_text

        raise Exception("An error occurred: Unexpected response type")

    def handle_tool_use(self, func_name, func_params):
        if func_name == "get_quote":
            premium = get_quote(**func_params)
            return f"Quote generated: ${premium:.2f} per month"

        raise Exception("An unexpected tool was used")
```

> 📝 原文此处还提供了 TypeScript、C#、Go、Java、PHP、Ruby 的等价实现，逻辑完全一致，详见[原文](https://platform.claude.com/docs/en/about-claude/use-case-guides/customer-support-chat)。

### 3.5 构建用户界面

用 Streamlit 搭建聊天界面（Streamlit 是 Python 框架，故这部分仅 Python 演示；上面的 ChatBot 类才是可移植到任何语言的核心）。创建 `app.py`：

```python
import streamlit as st
from chatbot import ChatBot
from config import TASK_SPECIFIC_INSTRUCTIONS


def main():
    st.title("Chat with Eva, Acme Insurance Company's Assistant🤖")

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "user", "content": TASK_SPECIFIC_INSTRUCTIONS},
            {"role": "assistant", "content": "Understood"},
        ]

    chatbot = ChatBot(st.session_state)

    # Display user and assistant messages skipping the first two
    for message in st.session_state.messages[2:]:
        # ignore tool use blocks
        if isinstance(message["content"], str):
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    if user_msg := st.chat_input("Type your message here..."):
        st.chat_message("user").markdown(user_msg)

        with st.chat_message("assistant"):
            with st.spinner("Eva is thinking..."):
                response_placeholder = st.empty()
                full_response = chatbot.process_user_input(user_msg)
                response_placeholder.markdown(full_response)


if __name__ == "__main__":
    main()
```

运行程序：

```bash
streamlit run app.py
```

### 3.6 评估提示词

提示词通常要经过反复测试和优化才能达到生产可用水准。要判断方案是否就绪，需要用**定量与定性相结合**的系统化流程评估聊天机器人的表现。基于前面定义的成功标准，创建一套[扎实的实证评估](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests#build-evaluations)，才能持续优化提示词。

---

## 四、性能优化策略

在复杂场景下，除了常规[提示词工程技巧](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview)和[护栏实施策略](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)，还有以下常见优化场景：

### 4.1 用 RAG 降低长上下文延迟

* **问题**：把所有静态和动态信息塞进提示词，会导致成本高、响应慢，甚至触及上下文窗口上限。
* **方案**：用 [embedding 模型](https://platform.claude.com/docs/en/build-with-claude/embeddings)（如 MongoDB 旗下 Voyage AI 的模型）把信息转成向量，根据当前提问动态检索相关内容，而不是每次塞入全部上下文。
* **效果**：在上下文需求量大的客服系统中，RAG 已被证明可以提高准确性、缩短响应时间、降低 API 成本。完整示例见 [RAG 教程](https://platform.claude.com/cookbook/capabilities-retrieval-augmented-generation-guide)。

### 4.2 用工具调用集成实时数据

* **问题**：需要实时信息的咨询（账户余额、保单详情等），基于 embedding 的 RAG 方案不够用。
* **方案**：用工具调用查询客户信息、获取订单详情、代客户取消订单等，把实时数据集成进 Claude 的回复。详见[工具调用：客服智能体教程](https://platform.claude.com/cookbook/tool-use-customer-service-agent)。

### 4.3 强化输入与输出护栏

部署客服机器人必须防范滥用、越界提问和不当回复。Claude 本身对此有较强抵抗力，还可额外采取：

* **[减少幻觉](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)**：实施事实核查机制和[引用（citations）](https://platform.claude.com/cookbook/misc-using-citations)功能，让回复有据可依。
* **交叉核对信息**：验证智能体的回复是否符合公司政策和已知事实。
* **避免契约性承诺**：确保智能体不会做出未经授权的承诺或达成协议。
* **[防范越狱攻击](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/mitigate-jailbreaks)**：用无害性筛查、输入校验等方法，防止用户利用模型漏洞生成不当内容。
* **避免提及竞争对手**：实施竞品提及过滤器，保持品牌聚焦。
* **[提升输出一致性](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/increase-consistency)**：即使在冗长复杂的交互中，也防止 Claude 改变风格或"人设崩坏"。
* **移除个人身份信息（PII）**：除非有明确要求和授权，否则应从回复中剔除任何 PII。

### 4.4 用流式输出降低感知响应时间

回复较长时，流式输出让用户渐进式地看到答案，而不是干等整个回复生成完毕。实现步骤：

1. 使用 [Anthropic Streaming API](https://platform.claude.com/docs/en/build-with-claude/streaming) 支持流式响应。
2. 配置前端以处理逐步到达的文本块。
3. 每收到一个文本块就即时显示，模拟实时打字效果。
4. 实现保存完整回复的机制，让用户离开页面再回来时仍能查看。

某些情况下，流式输出让你可以选用基础延迟更高、但更先进的模型——渐进式展示能缓解较长处理时间的体感影响。

### 4.5 扩展聊天机器人的架构

复杂度提升时，架构也需要演进。但在加更多层级之前，先考虑更轻量的选项：

1. 确保已把提示词的潜力挖到极致（参考[提示词工程指南](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview)）。
2. 给提示词增加更多[工具](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)（可包括[提示词链](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices#chain-complex-prompts)），看能否实现所需功能。

如果机器人需要处理差异极大的任务类型，可以增加一个[独立的意图分类器](https://platform.claude.com/cookbook/capabilities-classification-guide)来路由初始提问：客户提问先经分类器，再被路由到各自的专门对话流程（每个流程有自己的一套工具和 system prompt）。注意：这需要额外调用一次 Claude，会增加延迟。

---

## 五、集成到正式客服工作流

前面的示例聚焦 Streamlit 环境中的 Python 函数，但要真正部署实时客服机器人，需要一个 API 服务：

1. **创建 API 封装层**：用 Flask 或 FastAPI 把代码包装成 HTTP 服务，接收用户输入并完整返回 Assistant 的回复。服务可具备以下特性：
   * **Server-Sent Events（SSE）**：服务器向客户端实时流式推送响应，提供流畅的交互体验。
   * **缓存**：提升响应速度，减少不必要的 API 调用。
   * **上下文保持**：用户离开页面再回来时仍能维持对话上下文，保证对话连贯。
2. **构建 Web 界面**：实现易用的 Web UI，供用户与 Claude 驱动的智能体交互。

---

## 六、下一步

| 主题 | 说明 | 链接 |
|------|------|------|
| 工具调用 | 让 Claude 访问你的 API，代客户执行操作 | [Tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) |
| 开发测试 | 构建评估体系，对照成功标准衡量智能体表现 | [Develop tests](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests) |
| 流式输出 | 让客户实时看到答案逐字生成 | [Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) |
| 提示词工程 | 打磨 system prompt 和示例，提升任务表现 | [Prompt engineering](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/overview) |
