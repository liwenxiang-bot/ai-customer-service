import { useEffect, useState } from "react";
import {
  Button, Card, Form, Input, InputNumber, Switch, Space, Divider, Progress,
  Alert, Row, Col, App as AntApp, Tag, Popconfirm, Typography, Segmented, Collapse, Table, Modal,
} from "antd";
import { SyncOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { aiConfigApi, knowledgeApi, evalCaseApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, isAdmin } from "../auth";
import { usePolling } from "../hooks/usePolling";

// 检索严格度档位 → (重排分下限, 向量相似度下限)。均衡 = 线上校准默认值。
const PRESETS = [
  { key: "strict", label: "严格", min_score: 0.25, vector_min_sim: 0.55 },
  { key: "balanced", label: "均衡", min_score: 0.15, vector_min_sim: 0.5 },
  { key: "loose", label: "宽松", min_score: 0.1, vector_min_sim: 0.45 },
];

export function AIConfig() {
  const { user } = useAuth();
  const admin = isAdmin(user?.role);
  const { message, modal } = AntApp.useApp();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [rebuild, setRebuild] = useState<any>(null);
  const [guardDefaults, setGuardDefaults] = useState<{ scope?: string; reply?: string }>({});
  const guardOn = Form.useWatch("guard_enabled", form);
  // 评测与校准
  const [evalCases, setEvalCases] = useState<any[]>([]);
  const [evalTotal, setEvalTotal] = useState<number | null>(null);
  const [casesOpen, setCasesOpen] = useState(false);
  const [calib, setCalib] = useState<any>(null);
  const [calibrating, setCalibrating] = useState(false);
  const loadCases = () => evalCaseApi.list().then((d) => { setEvalCases(d.items || []); setEvalTotal(d.total ?? 0); }).catch(() => {});
  useEffect(() => { loadCases(); }, []);
  const removeCase = async (id: string) => {
    try { await evalCaseApi.remove(id); loadCases(); } catch (e) { message.error(apiError(e)); }
  };
  const runCalibration = async () => {
    setCalibrating(true);
    try {
      setCalib(await aiConfigApi.calibrate());
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setCalibrating(false);
    }
  };

  const load = async () => {
    try {
      const cfg = await aiConfigApi.get();
      form.setFieldsValue(cfg);
      setGuardDefaults({ scope: cfg.default_business_scope, reply: cfg.default_off_topic_reply });
    } catch (e) {
      message.error(apiError(e));
    }
  };
  useEffect(() => { load(); }, []);

  const fetchRebuild = () =>
    aiConfigApi.rebuildStatus().then((d) => setRebuild(d.rebuild)).catch(() => { /* 轮询忽略瞬时错误 */ });
  // 进页面先查一次是否有进行中的重建。
  useEffect(() => { fetchRebuild(); }, []);
  const rebuilding = !!rebuild && (rebuild.status === "running" || rebuild.status === "pending");
  // 仅在重建进行中时以 2s 轮询进度；页面隐藏自动暂停（usePolling 内置），空闲时不发请求。
  usePolling(fetchRebuild, 2000, { enabled: rebuilding });

  const doSave = async (values: any) => {
    setLoading(true);
    try {
      const res = await aiConfigApi.update(values);
      if (res.rebuild) {
        message.warning("Embedding 模型已变更，正在后台全量重建向量…");
        fetchRebuild();
      } else {
        message.success("已保存");
      }
      load();
    } catch (e: any) {
      // Embedding 模型/维度变更被后端 409 拦截：弹确认框展示预检数字，确认后带 confirm 重发。
      const detail = e?.response?.status === 409 ? e?.response?.data?.detail : null;
      if (detail?.code === "embedding_rebuild_confirm") {
        modal.confirm({
          title: "确认更换 Embedding 模型 / 维度？",
          content: `将触发全量重建：清空并重算 ${detail.items} 个条目 / ${detail.chunks} 个分块的向量，期间向量检索降级为关键词检索。`,
          okText: "确认重建",
          okButtonProps: { danger: true },
          cancelText: "取消",
          onOk: () => doSave({ ...values, confirm_rebuild: true }),
        });
      } else {
        message.error(apiError(e));
      }
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    const v = await form.validateFields();
    await doSave(v);
  };

  const testLLM = async () => {
    try {
      const r = await aiConfigApi.testLLM("你好，请用一句话自我介绍");
      message.success(`✅ 调用成功：${r.reply?.slice(0, 60)}`);
    } catch (e) {
      message.error(apiError(e));
    }
  };

  const doRebuild = async () => {
    try {
      await knowledgeApi.rebuildAll();
      message.info("已触发向量重建，进度见上方");
      fetchRebuild();
    } catch (e) {
      message.error(apiError(e));
    }
  };

  // 重建"完成但有缺口"时的定点补救：只重嵌缺向量的条目，不动已就绪的。
  const reembedPending = async () => {
    try {
      const r = await knowledgeApi.reembedPending();
      message.info(`已重新排队 ${r.queued} 个待补条目`);
      fetchRebuild();
    } catch (e) {
      message.error(apiError(e));
    }
  };

  return (
    <div>
      <div className="acs-page-title">AI 配置</div>
      <div className="acs-page-sub">切换对话 / 向量 / 重排模型与参数，设置客服人设与检索策略；更换向量模型会触发全量重建。</div>

      {rebuilding && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 16 }}
          message="向量重建进行中（重建期间检索自动降级，不影响服务）"
          description={<Progress percent={Math.round((rebuild.progress || 0) * 100)} status="active" />}
        />
      )}
      {rebuild?.status === "failed" && (
        <Alert
          type="error" showIcon style={{ marginBottom: 16 }}
          message={`上次向量重建失败：${rebuild.error}`}
          description={
            <Progress
              percent={Math.round((rebuild.progress || 0) * 100)} status="exception"
              format={() => `${rebuild.processed ?? 0}/${rebuild.total ?? 0} 分块`}
            />
          }
          action={admin && <Button size="small" danger onClick={doRebuild}>重试重建</Button>}
        />
      )}
      {rebuild?.status === "completed" && rebuild.error && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 16 }}
          message="上次向量重建完成，但部分分块缺向量"
          description={rebuild.error}
          action={admin && <Button size="small" onClick={reembedPending}>补齐缺失向量</Button>}
        />
      )}

      <Form form={form} layout="vertical" disabled={!admin}>
        <Row gutter={16}>
          <Col xs={24} lg={12}>
            <Card title="大语言模型 (LLM)" extra={admin && <Button size="small" icon={<ThunderboltOutlined />} onClick={testLLM}>测试连接</Button>}>
              <Form.Item name="llm_provider" label="Provider 标识"><Input placeholder="openai / deepseek / qwen ..." /></Form.Item>
              <Form.Item name="llm_base_url" label="Base URL"><Input placeholder="https://api.openai.com/v1" /></Form.Item>
              <Form.Item name="llm_api_key" label="API Key"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
              <Form.Item name="llm_model" label="模型"><Input placeholder="gpt-4o-mini" /></Form.Item>
              <Space size="large">
                <Form.Item name="llm_temperature" label="Temperature"><InputNumber min={0} max={2} step={0.1} /></Form.Item>
                <Form.Item name="llm_max_tokens" label="Max Tokens"><InputNumber min={64} max={8192} /></Form.Item>
              </Space>
            </Card>

            <Card title="System Prompt / 人设" style={{ marginTop: 16 }}>
              <Form.Item name="system_prompt" noStyle>
                <Input.TextArea rows={10} placeholder="客服助手的角色设定与行为准则" />
              </Form.Item>
            </Card>

            <Card
              title="防滥用 / 服务范围"
              style={{ marginTop: 16 }}
              extra={
                <Form.Item name="guard_enabled" valuePropName="checked" noStyle>
                  <Switch checkedChildren="开启" unCheckedChildren="关闭" />
                </Form.Item>
              }
            >
              <div style={{ color: "var(--ant-color-text-secondary)", fontSize: 13, marginBottom: 12 }}>
                只回答业务范围内的问题：越狱话术直接拦截、超长消息提示精简；与业务无关的请求（写代码、写作业、闲聊等）
                由模型识别后自动回复下方话术，不再进行第二次模型调用。
              </div>
              <Form.Item
                name="business_scope"
                label="业务范围"
                tooltip="一句话描述客服负责的内容，模型据此判断是否离题。留空使用默认描述。"
              >
                <Input.TextArea rows={2} maxLength={500} showCount disabled={!guardOn} placeholder={guardDefaults.scope} />
              </Form.Item>
              <Form.Item name="off_topic_reply" label="离题拒答话术" tooltip="留空使用默认话术。">
                <Input.TextArea rows={3} maxLength={500} showCount disabled={!guardOn} placeholder={guardDefaults.reply} />
              </Form.Item>
              <Form.Item name="max_input_chars" label="单条消息最大字数" tooltip="超过时提示用户精简，不会调用模型。">
                <InputNumber min={100} max={8000} step={100} disabled={!guardOn} style={{ width: 160 }} addonAfter="字" />
              </Form.Item>
            </Card>
          </Col>

          <Col xs={24} lg={12}>
            <Card title="Embedding（向量化）">
              <Alert type="info" showIcon style={{ marginBottom: 12 }} message="切换 Embedding 模型 / 维度会触发全量向量重建。" />
              <Form.Item name="embedding_base_url" label="Base URL"><Input /></Form.Item>
              <Form.Item name="embedding_api_key" label="API Key"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
              <Space size="large">
                <Form.Item name="embedding_model" label="模型"><Input placeholder="text-embedding-3-small" /></Form.Item>
                <Form.Item name="embedding_dim" label="维度" tooltip="必须与服务器 .env 的 EMBEDDING_DIM 一致（向量列按它建表）。换不同维度的模型：先改 EMBEDDING_DIM 并重启 backend 与 worker，再回这里切换。"><InputNumber min={64} max={4096} /></Form.Item>
              </Space>
              {admin && (
                <div style={{ marginTop: 8 }}>
                  <Popconfirm
                    title="全量重建向量？"
                    description="按当前分块参数与嵌入模型重切、重嵌全部条目，期间向量检索降级为关键词检索。"
                    okText="开始重建"
                    cancelText="取消"
                    onConfirm={doRebuild}
                  >
                    <Button icon={<SyncOutlined />} disabled={rebuilding}>全量重建向量</Button>
                  </Popconfirm>
                  <div style={{ marginTop: 4 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      修改分块大小 / 重叠或嵌入模型后，用于批量重算存量条目的向量。
                    </Typography.Text>
                  </div>
                </div>
              )}
            </Card>

            <Card title="Rerank（重排）" style={{ marginTop: 16 }}>
              <Form.Item name="rerank_enabled" label="启用重排" valuePropName="checked"><Switch /></Form.Item>
              <Form.Item name="rerank_base_url" label="Base URL"><Input /></Form.Item>
              <Form.Item name="rerank_api_key" label="API Key"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
              <Form.Item name="rerank_model" label="模型"><Input placeholder="bge-reranker-v2-m3" /></Form.Item>
            </Card>

            <Card title="检索严格度" style={{ marginTop: 16 }}>
              <Form.Item noStyle shouldUpdate={(p, c) => p?.retrieval?.min_score !== c?.retrieval?.min_score || p?.retrieval?.vector_min_sim !== c?.retrieval?.vector_min_sim}>
                {() => {
                  const ms = form.getFieldValue(["retrieval", "min_score"]);
                  const vs = form.getFieldValue(["retrieval", "vector_min_sim"]);
                  const cur = PRESETS.find((x) => Math.abs(x.min_score - ms) < 1e-9 && Math.abs(x.vector_min_sim - vs) < 1e-9)?.key || "custom";
                  return (
                    <>
                      <Segmented
                        value={cur}
                        options={[...PRESETS.map((x) => ({ label: x.label, value: x.key })), { label: "自定义", value: "custom", disabled: true }]}
                        onChange={(k) => {
                          const px = PRESETS.find((x) => x.key === k);
                          if (px) form.setFieldsValue({ retrieval: { ...form.getFieldValue("retrieval"), min_score: px.min_score, vector_min_sim: px.vector_min_sim } });
                        }}
                      />
                      <div style={{ fontSize: 12, color: "#6b7280", marginTop: 8, lineHeight: 1.8 }}>
                        <div><b>严格</b>：宁可说「没有相关资料」也不拿沾边内容作答；<b>均衡</b>：默认，按线上数据校准；<b>宽松</b>：尽量给资料，误引用会增多。</div>
                        <div>当前门槛：重排分下限 {ms ?? "—"} · 向量相似度下限 {vs ?? "—"}。不确定该选哪档？用下方「评测与校准」让数据来定。</div>
                      </div>
                    </>
                  );
                }}
              </Form.Item>
              <Collapse
                ghost style={{ marginTop: 12 }}
                items={[{
                  key: "adv",
                  label: <span style={{ fontSize: 13 }}>高级参数（一般不用改）</span>,
                  children: (
                    <>
                      <Row gutter={12}>
                        <Col span={8}><Form.Item name={["retrieval", "min_score"]} label="重排分下限" tooltip="启用重排时的最终门槛：重排分低于此值的资料丢弃。对题的模糊问题常只有 0.17–0.30，误命中的沾边资料 0.16–0.19，所以请用校准功能定，不要凭感觉调。"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "vector_min_sim"]} label="向量相似度下限" tooltip="向量召回的余弦相似度门槛；重排开启时只做预筛。按嵌入模型校准：text-embedding-v4 约 0.5–0.6，OpenAI text-embedding-3 约 0.2–0.35。"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "top_k"]} label="Top-K" tooltip="最多注入给模型的资料条数。只影响上下文长度，不影响第一名是谁。"><InputNumber min={1} max={20} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "vector_weight"]} label="向量权重" tooltip="融合排序中向量路径的权重。重排开启时几乎无感。"><InputNumber min={0} max={1} step={0.1} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "keyword_weight"]} label="关键词权重" tooltip="融合排序中关键词路径的权重。重排开启时几乎无感。"><InputNumber min={0} max={1} step={0.1} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "candidate_multiplier"]} label="候选倍数" tooltip="进入融合/重排的候选数 = Top-K × 此值。知识不足几百条时永远是全部，改了也无感。"><InputNumber min={1} max={50} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "trgm_threshold"]} label="Trigram 阈值" tooltip="只影响英文/编码类子串匹配（型号、错误码）的宽松程度。"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "chunk_size"]} label="分块大小" tooltip="每块字符数。只对长文档有意义；改动后需在知识库「全量重建向量」才生效。"><InputNumber min={100} max={2000} style={{ width: "100%" }} /></Form.Item></Col>
                        <Col span={8}><Form.Item name={["retrieval", "chunk_overlap"]} label="重叠" tooltip="相邻块的重叠字符数。改动后需全量重建向量。"><InputNumber min={0} max={500} style={{ width: "100%" }} /></Form.Item></Col>
                      </Row>
                      <Form.Item name={["retrieval", "expand_context"]} label="上下文扩展" valuePropName="checked" tooltip="命中分块时附带其相邻分块（small-to-big）。单块的短条目无感。"><Switch /></Form.Item>
                    </>
                  ),
                }]}
              />
              <Space size="large">
                <Form.Item name="content_safety_enabled" label="内容安全过滤" valuePropName="checked"><Switch /></Form.Item>
                <Form.Item name="semantic_cache_enabled" label="语义缓存" valuePropName="checked" tooltip="高频重复问题直接返回缓存答案。咨询量小时收益小、缓存错答案的风险大，默认关。"><Switch /></Form.Item>
              </Space>
            </Card>

            <Card
              title="评测与校准" style={{ marginTop: 16 }}
              extra={<Button size="small" onClick={() => setCasesOpen(true)}>管理用例{evalTotal != null ? `（${evalTotal}）` : ""}</Button>}
            >
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 12, lineHeight: 1.8 }}>
                评测用例来自知识库「测试检索」弹窗：输入客户问法后点「应命中这条」或「应该无结果」。用例够 20 条以上时，点「运行校准」，系统会对每个门槛值算出「该命中的命中了多少、该拒绝的拒绝了多少」，并给出推荐值。
              </div>
              <Space wrap>
                <Button type="primary" icon={<ThunderboltOutlined />} loading={calibrating} disabled={!admin || !evalTotal} onClick={runCalibration}>运行校准</Button>
                {calib?.recommended != null && (
                  <Button
                    disabled={!admin} loading={loading}
                    onClick={() => {
                      form.setFieldsValue({ retrieval: { ...form.getFieldValue("retrieval"), [calib.param]: calib.recommended } });
                      doSave(form.getFieldsValue());
                    }}
                  >
                    应用推荐值 {calib.param === "min_score" ? "重排分下限" : "向量相似度下限"} = {calib.recommended} 并保存
                  </Button>
                )}
              </Space>
              {calib?.error && <Alert style={{ marginTop: 12 }} type="warning" showIcon message={calib.error} />}
              {calib && !calib.error && calib.cases > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 6 }}>
                    {calib.cases} 条用例（应命中 {calib.positives} · 应无结果 {calib.negatives}），按{calib.param === "min_score" ? "重排分下限" : "向量相似度下限"}扫描；当前值 {calib.current}
                    {calib.current_eval?.accuracy != null ? `，准确率 ${(calib.current_eval.accuracy * 100).toFixed(0)}%` : ""}。
                  </div>
                  <Table
                    size="small" pagination={false} rowKey="value" dataSource={calib.sweep}
                    rowClassName={(r: any) => (r.value === calib.recommended ? "acs-row-recommended" : "")}
                    columns={[
                      { title: "门槛值", dataIndex: "value", width: 90, render: (v: number) => <b>{v}{v === calib.recommended ? " ★" : ""}</b> },
                      { title: "应命中→命中", render: (_: any, r: any) => r.positives ? `${r.hits}/${r.positives}（${(r.hit_rate * 100).toFixed(0)}%）` : "—" },
                      { title: "应无结果→拒绝", render: (_: any, r: any) => r.negatives ? `${r.rejects}/${r.negatives}（${(r.reject_rate * 100).toFixed(0)}%）` : "—" },
                      { title: "准确率", dataIndex: "accuracy", width: 90, render: (v: number) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`) },
                    ]}
                  />
                  {calib.per_case?.some((c: any) => !c.ok_at_current) && (
                    <div style={{ marginTop: 10, fontSize: 12 }}>
                      <div style={{ color: "#6b7280", marginBottom: 4 }}>当前门槛下不通过的用例：</div>
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {calib.per_case.filter((c: any) => !c.ok_at_current).slice(0, 12).map((c: any, i: number) => (
                          <li key={i}>「{c.query}」期望 {c.expected_title || "命中"}，实际第一名 {c.top_title ?? "无结果"}{c.top_score != null ? `（${c.top_score}）` : ""}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              )}
            </Card>

            <Modal title="评测用例" open={casesOpen} onCancel={() => setCasesOpen(false)} footer={null} width={720}>
              <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 8 }}>在知识库「测试检索」里标注新增；这里只做查看与删除。</div>
              <Table
                size="small" rowKey="id" dataSource={evalCases} pagination={{ pageSize: 10, showSizeChanger: false }}
                columns={[
                  { title: "客户问法", dataIndex: "query" },
                  { title: "期望", dataIndex: "expected_title", width: 200, render: (t: string, r: any) => (r.expected_item_id ? t || "（条目）" : <Tag>应无结果</Tag>) },
                  { title: "来源", dataIndex: "source", width: 80 },
                  { title: "", width: 70, render: (_: any, r: any) => <Popconfirm title="删除这条用例？" onConfirm={() => removeCase(r.id)}><Button type="link" size="small" danger>删除</Button></Popconfirm> },
                ]}
              />
            </Modal>
          </Col>
        </Row>

        {admin && (
          <div style={{ position: "sticky", bottom: 0, padding: "12px 0", textAlign: "right" }}>
            <Button type="primary" size="large" loading={loading} onClick={save}>保存配置</Button>
          </div>
        )}
      </Form>
    </div>
  );
}
