import { FormEvent, useEffect, useMemo, useState } from "react";

import { EvaluationPanel } from "./components/EvaluationPanel";
import { MessageList } from "./components/MessageList";
import { SampleQuestions } from "./components/SampleQuestions";
import { TracePanel } from "./components/TracePanel";
import { getDemoScenarioSet } from "./data/demoScenarios";
import { useAgentChat } from "./hooks/useAgentChat";
import type { AgentCapabilityManifest, AgentFeatureKey, ChatResponse, DemoUser, DemoUserCreatePayload, ReasoningView } from "./types/api";

type LearningToggles = {
  tools: boolean;
  rag: boolean;
  reasoning: boolean;
};

export default function App() {
  const {
    users,
    selectedUser,
    selectedUserId,
    messages,
    isLoading,
    isResuming,
    isEvaluating,
    isSubmittingFeedback,
    isCreatingUser,
    activeResponse,
    traceEvents,
    evalReport,
    evalError,
    feedbackReport,
    feedbackError,
    resumeResult,
    agentBaseUrl,
    agentCapabilities,
    capabilitiesError,
    selectUser,
    createUser,
    sendMessage,
    resumeWorkflow,
    runEval,
    submitFeedback,
  } = useAgentChat();
  const scenarioSet = useMemo(
    () => getDemoScenarioSet(selectedUserId),
    [selectedUserId, agentCapabilities.lesson?.number],
  );
  const [draft, setDraft] = useState(scenarioSet.defaultQuestion);
  const [toggles, setToggles] = useState<LearningToggles>({
    tools: false,
    rag: false,
    reasoning: false,
  });
  const learningAvailability = {
    tools: isFeatureEnabled(agentCapabilities, "tool_calls"),
    rag: isFeatureEnabled(agentCapabilities, "rag_citations"),
    reasoning: isFeatureEnabled(agentCapabilities, "reasoning_content"),
  };
  const reasoningView: ReasoningView =
    toggles.reasoning && learningAvailability.reasoning
      ? "teaching"
      : (toggles.tools && learningAvailability.tools) || (toggles.rag && learningAvailability.rag)
        ? "summary"
        : "off";

  useEffect(() => {
    setDraft(scenarioSet.defaultQuestion);
  }, [scenarioSet.defaultQuestion]);

  useEffect(() => {
    setToggles((current) => ({
      tools: current.tools && learningAvailability.tools,
      rag: current.rag && learningAvailability.rag,
      reasoning: current.reasoning && learningAvailability.reasoning,
    }));
  }, [learningAvailability.tools, learningAvailability.rag, learningAvailability.reasoning]);

  function toggleLearning(key: keyof LearningToggles) {
    if (!learningAvailability[key]) {
      return;
    }
    setToggles((current) => ({ ...current, [key]: !current[key] }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (isLoading || !draft.trim()) {
      return;
    }
    await sendMessage(draft.trim(), reasoningView);
  }

  async function handleClarificationCandidateSelect(candidateValue: string) {
    if (isLoading || !candidateValue.trim()) {
      return;
    }
    const followUp = buildClarificationFollowUp(candidateValue.trim(), activeResponse);
    setDraft(followUp);
    await sendMessage(followUp, reasoningView);
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <div>
          <p className="eyebrow">HUANGYE OUTDOOR / SERVICE LAB</p>
          <h1>荒野户外 · 评测工作台</h1>
          <p className="header-description">从商品咨询到售后复核，查看每次答复的业务事实、政策依据与执行记录。</p>
        </div>
        <img className="camp-illustration" src="/cartoon-camp.svg" alt="" aria-hidden="true" width="520" height="290" />
      </header>

      <div className="workbench-route" aria-label="服务流程概览">
        <span>01 识别需求</span><span>02 核对事实</span><span>03 查询规则 / 校验资格</span><span>04 答复 / 待审核</span>
      </div>

      <AgentTargetBanner baseUrl={agentBaseUrl} capabilities={agentCapabilities} error={capabilitiesError} />

      <UserSwitcher
        users={users}
        selectedUserId={selectedUserId}
        selectedUser={selectedUser}
        isCreatingUser={isCreatingUser}
        onSelectUser={selectUser}
        onCreateUser={createUser}
      />

      <section className="top-grid" aria-label="对话与调试">
        <div className="service-column">
          <MessageList messages={messages} isLoading={isLoading} />
        <form className="panel composer-panel" onSubmit={handleSubmit}>
          <div className="panel-header">
            <span>输入用户问题</span>
            <small>选择业务问题；按需展开工具、政策与执行详情</small>
          </div>

          <label htmlFor="question">客户问题</label>
          <textarea
            id="question"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={isLoading}
            rows={4}
            placeholder="例如：SO20260601090000008-a1000008 还没发货，我现在能退款吗？"
          />

          <div className="composer-actions">
            <div className="switch-group">
              <div className="learning-switch" aria-label="执行详情展示选项">
                <button
                  className={toggles.tools ? "active" : ""}
                  disabled={!learningAvailability.tools}
                  onClick={() => toggleLearning("tools")}
                  type="button"
                  aria-pressed={toggles.tools}
                >
                  <span>工具调用</span>
                  <small>{learningAvailability.tools ? "显示 Action、Observation 和 Tool Trace" : "当前版本未开放"}</small>
                </button>
                <button
                  className={toggles.rag ? "active" : ""}
                  disabled={!learningAvailability.rag}
                  onClick={() => toggleLearning("rag")}
                  type="button"
                  aria-pressed={toggles.rag}
                >
                  <span>RAG 引用</span>
                  <small>{learningAvailability.rag ? "突出知识库命中和 citations" : "当前版本未开放"}</small>
                </button>
                <button
                  className={toggles.reasoning ? "active" : ""}
                  disabled={!learningAvailability.reasoning}
                  onClick={() => toggleLearning("reasoning")}
                  type="button"
                  aria-pressed={toggles.reasoning}
                >
                  <span>Reasoning</span>
                  <small>{learningAvailability.reasoning ? "展示 reasoning_content 和执行链路" : "当前版本未开放"}</small>
                </button>
              </div>
            </div>
            <button className="submit-button" disabled={isLoading} type="submit">
              {isLoading ? "处理中..." : "发送给 Agent"}
            </button>
          </div>
        </form>
        </div>
        <TracePanel
          response={activeResponse}
          traces={traceEvents}
          selectedReasoningView={reasoningView}
          toggles={toggles}
          resumeResult={resumeResult}
          isResuming={isResuming}
          isChatLoading={isLoading}
          capabilities={agentCapabilities}
          onClarificationCandidateSelect={handleClarificationCandidateSelect}
          onResumeWorkflow={resumeWorkflow}
        />
      </section>

      <section className="scenario-section" aria-label="业务场景示例">


        <SampleQuestions scenarioSet={scenarioSet} disabled={isLoading} onSelect={setDraft} />
      </section>

      <details className="evaluation-drawer">
        <summary>回归评测与反馈 · 检查关键业务路径</summary>
      <EvaluationPanel
        report={evalReport}
        error={evalError}
        feedbackReport={feedbackReport}
        feedbackError={feedbackError}
        isEvaluating={isEvaluating}
        isSubmittingFeedback={isSubmittingFeedback}
        capabilities={agentCapabilities}
        onRunEval={runEval}
        onSubmitFeedback={submitFeedback}
      />
      </details>
    </main>
  );
}

function AgentTargetBanner({
  baseUrl,
  capabilities,
  error,
}: {
  baseUrl: string;
  capabilities: AgentCapabilityManifest;
  error?: string;
}) {
  const lessonTitle = capabilities.agent?.name || "荒野户外售后服务助手";
  const lessonId = "演示环境 · 模拟售后";
  const enabledFeatures = Object.entries(capabilities.features ?? {}).filter(([, enabled]) => enabled).length;
  const totalFeatures = Object.keys(capabilities.features ?? {}).length;

  return (
    <section className="capability-banner" aria-label="当前 Agent 能力配置">
      <div>
        <strong>{lessonTitle}</strong>
        <span>
          {lessonId} · {baseUrl}
        </span>
      </div>
      <div className="capability-banner-meta">
        <span>{totalFeatures ? `${enabledFeatures}/${totalFeatures} 项能力开放` : "未声明能力"}</span>
        {error ? <span className="capability-warning">{error}</span> : null}
      </div>
    </section>
  );
}

function isFeatureEnabled(capabilities: AgentCapabilityManifest, feature: AgentFeatureKey) {
  return capabilities.features?.[feature] === true;
}

function buildClarificationFollowUp(candidateValue: string, response?: ChatResponse) {
  const clarificationField = stringValue((response?.clarification as Record<string, unknown> | undefined)?.clarification_field);
  const intent = response?.intent ?? stringValue(response?.session_state.intent);
  if (clarificationField === "sku") {
    return `查 ${candidateValue} 的库存和价格`;
  }
  if (intent === "refund_status_query" || intent?.includes("refund")) {
    return `查 ${candidateValue} 的退款进度`;
  }
  if (intent === "order_query" || intent?.includes("order")) {
    return `查 ${candidateValue} 的物流到哪了`;
  }
  return `我选择 ${candidateValue}`;
}

function stringValue(value: unknown) {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function UserSwitcher({
  users,
  selectedUserId,
  selectedUser,
  isCreatingUser,
  onSelectUser,
  onCreateUser,
}: {
  users: DemoUser[];
  selectedUserId: string;
  selectedUser: DemoUser;
  isCreatingUser: boolean;
  onSelectUser: (userId: string) => void;
  onCreateUser: (payload: DemoUserCreatePayload) => Promise<void>;
}) {
  const [isAdding, setIsAdding] = useState(false);
  const [form, setForm] = useState<DemoUserCreatePayload>({
    userId: "",
    nickname: "",
    memberLevel: "normal",
    riskLevel: "low",
    preferredCategories: "",
    preferredDelivery: "",
    budgetMin: undefined,
    budgetMax: undefined,
    invoiceRequired: false,
  });
  const selectedPreferences = selectedUser.preferences;

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    if (!form.userId.trim() || !form.nickname.trim()) {
      return;
    }
    await onCreateUser({
      ...form,
      userId: form.userId.trim(),
      nickname: form.nickname.trim(),
      preferredCategories: form.preferredCategories?.trim(),
      preferredDelivery: form.preferredDelivery?.trim(),
    });
    setIsAdding(false);
    setForm({
      userId: "",
      nickname: "",
      memberLevel: "normal",
      riskLevel: "low",
      preferredCategories: "",
      preferredDelivery: "",
      budgetMin: undefined,
      budgetMax: undefined,
      invoiceRequired: false,
    });
  }

  return (
    <section className="panel user-panel" aria-label="演示用户">
      <div className="panel-header">
        <span>演示用户</span>
        <small>切换用户会切换 Runtime Context、聊天历史和 trace</small>
      </div>
      <div className="user-layout">
        <div className="user-tabs">
          {users.map((user) => (
            <button
              key={user.profile.userId}
              type="button"
              className={user.profile.userId === selectedUserId ? "active" : ""}
              onClick={() => onSelectUser(user.profile.userId)}
            >
              <strong>{user.profile.nickname}</strong>
              <small>
                {user.profile.userId} · {({ normal: "普通会员", silver: "行者会员", gold: "山友会员" } as Record<string, string>)[user.profile.memberLevel] ?? user.profile.memberLevel} · risk {user.profile.riskLevel}
              </small>
            </button>
          ))}
          <button type="button" className={isAdding ? "active add-user" : "add-user"} onClick={() => setIsAdding((value) => !value)}>
            <strong>新增用户</strong>
            <small>写入模拟电商后端</small>
          </button>
        </div>
        <div className="user-summary">
          <strong>
            {selectedUser.profile.nickname} / {selectedUser.profile.userId}
          </strong>
          <span>
            偏好：{selectedPreferences.preferredCategories || "未设置"} · 配送：
            {selectedPreferences.preferredDelivery || "未设置"} · 预算：
            {formatBudget(selectedPreferences.budgetMin, selectedPreferences.budgetMax)}
          </span>
        </div>
      </div>
      {isAdding ? (
        <form className="new-user-form" onSubmit={handleCreate}>
          <input
            value={form.userId}
            onChange={(event) => setForm((current) => ({ ...current, userId: event.target.value }))}
            placeholder="用户 ID，例如 U2001"
          />
          <input
            value={form.nickname}
            onChange={(event) => setForm((current) => ({ ...current, nickname: event.target.value }))}
            placeholder="昵称"
          />
          <select value={form.memberLevel} onChange={(event) => setForm((current) => ({ ...current, memberLevel: event.target.value }))}>
            <option value="normal">普通会员</option>
            <option value="silver">行者会员</option>
            <option value="gold">山友会员</option>
          </select>
          <select value={form.riskLevel} onChange={(event) => setForm((current) => ({ ...current, riskLevel: event.target.value }))}>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select>
          <input
            value={form.preferredCategories}
            onChange={(event) => setForm((current) => ({ ...current, preferredCategories: event.target.value }))}
            placeholder="偏好品类"
          />
          <input
            value={form.preferredDelivery}
            onChange={(event) => setForm((current) => ({ ...current, preferredDelivery: event.target.value }))}
            placeholder="配送偏好"
          />
          <input
            type="number"
            value={form.budgetMin ?? ""}
            onChange={(event) =>
              setForm((current) => ({ ...current, budgetMin: event.target.value ? Number(event.target.value) : undefined }))
            }
            placeholder="预算下限"
          />
          <input
            type="number"
            value={form.budgetMax ?? ""}
            onChange={(event) =>
              setForm((current) => ({ ...current, budgetMax: event.target.value ? Number(event.target.value) : undefined }))
            }
            placeholder="预算上限"
          />
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={Boolean(form.invoiceRequired)}
              onChange={(event) => setForm((current) => ({ ...current, invoiceRequired: event.target.checked }))}
            />
            <span>偏好电子发票</span>
          </label>
          <button type="submit" disabled={isCreatingUser}>
            {isCreatingUser ? "创建中..." : "创建用户"}
          </button>
        </form>
      ) : null}
    </section>
  );
}

function formatBudget(min?: number | null, max?: number | null) {
  if (typeof min === "number" && typeof max === "number") {
    return `${min}-${max}`;
  }
  if (typeof min === "number") {
    return `${min}+`;
  }
  if (typeof max === "number") {
    return `<=${max}`;
  }
  return "未设置";
}
