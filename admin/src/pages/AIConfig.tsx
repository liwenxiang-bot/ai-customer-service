import { useEffect, useState } from "react";
import {
  Button, Card, Form, Input, InputNumber, Switch, Space, Divider, Progress,
  Alert, Row, Col, App as AntApp, Tag,
} from "antd";
import { ThunderboltOutlined } from "@ant-design/icons";
import { aiConfigApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, isAdmin } from "../auth";

export function AIConfig() {
  const { user } = useAuth();
  const admin = isAdmin(user?.role);
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [rebuild, setRebuild] = useState<any>(null);

  const load = async () => {
    const cfg = await aiConfigApi.get();
    form.setFieldsValue(cfg);
  };
  useEffect(() => { load(); }, []);

  // Poll rebuild progress while one is running.
  useEffect(() => {
    const timer = setInterval(async () => {
      const d = await aiConfigApi.rebuildStatus();
      setRebuild(d.rebuild);
    }, 2000);
    aiConfigApi.rebuildStatus().then((d) => setRebuild(d.rebuild));
    return () => clearInterval(timer);
  }, []);

  const save = async () => {
    const v = await form.validateFields();
    setLoading(true);
    try {
      const res = await aiConfigApi.update(v);
      if (res.rebuild) message.warning("Embedding 模型已变更，正在后台全量重建向量…");
      else message.success("已保存");
      load();
    } catch (e) {
      message.error(apiError(e));
    } finally {
      setLoading(false);
    }
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
      await aiConfigApi.rebuild();
      message.info("已触发向量重建，进度见上方");
      aiConfigApi.rebuildStatus().then((d) => setRebuild(d.rebuild));
    } catch (e) {
      message.error(apiError(e));
    }
  };

  const rebuilding = rebuild && (rebuild.status === "running" || rebuild.status === "pending");

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
        <Alert type="error" showIcon style={{ marginBottom: 16 }} message={`上次向量重建失败：${rebuild.error}`}
          action={admin && <Button size="small" danger onClick={doRebuild}>重试重建</Button>} />
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
          </Col>

          <Col xs={24} lg={12}>
            <Card title="Embedding（向量化）">
              <Alert type="info" showIcon style={{ marginBottom: 12 }} message="切换 Embedding 模型 / 维度会触发全量向量重建。" />
              <Form.Item name="embedding_base_url" label="Base URL"><Input /></Form.Item>
              <Form.Item name="embedding_api_key" label="API Key"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
              <Space size="large">
                <Form.Item name="embedding_model" label="模型"><Input placeholder="text-embedding-3-small" /></Form.Item>
                <Form.Item name="embedding_dim" label="维度"><InputNumber min={64} max={4096} /></Form.Item>
              </Space>
            </Card>

            <Card title="Rerank（重排）" style={{ marginTop: 16 }}>
              <Form.Item name="rerank_enabled" label="启用重排" valuePropName="checked"><Switch /></Form.Item>
              <Form.Item name="rerank_base_url" label="Base URL"><Input /></Form.Item>
              <Form.Item name="rerank_api_key" label="API Key"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
              <Form.Item name="rerank_model" label="模型"><Input placeholder="bge-reranker-v2-m3" /></Form.Item>
            </Card>

            <Card title="检索参数与开关" style={{ marginTop: 16 }}>
              <Row gutter={12}>
                <Col span={8}><Form.Item name={["retrieval", "top_k"]} label="Top-K"><InputNumber min={1} max={20} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "vector_weight"]} label="向量权重"><InputNumber min={0} max={1} step={0.1} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "keyword_weight"]} label="关键词权重"><InputNumber min={0} max={1} step={0.1} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "chunk_size"]} label="分块大小"><InputNumber min={100} max={2000} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "chunk_overlap"]} label="重叠"><InputNumber min={0} max={500} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "rerank_top_n"]} label="重排 Top-N"><InputNumber min={1} max={20} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "vector_min_sim"]} label="向量相似度下限" tooltip="低于此余弦相似度的向量结果直接丢弃，避免“总能命中最近但不相关的内容”。按 embedding 模型校准：OpenAI 系约 0.2–0.35，BGE/Jina 系约 0.4–0.5。"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "min_score"]} label="重排分下限" tooltip="仅在启用重排时生效：重排得分低于此值的结果丢弃（0 表示不过滤）。"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "trgm_threshold"]} label="Trigram 阈值" tooltip="关键词/子串匹配的相似度下限，过滤弱噪声命中。"><InputNumber min={0} max={1} step={0.05} style={{ width: "100%" }} /></Form.Item></Col>
                <Col span={8}><Form.Item name={["retrieval", "candidate_multiplier"]} label="候选倍数" tooltip="融合/重排前的候选集大小 = Top-K × 此值，越大重排召回越好、开销略增。"><InputNumber min={1} max={50} style={{ width: "100%" }} /></Form.Item></Col>
              </Row>
              <Space size="large">
                <Form.Item name={["retrieval", "expand_context"]} label="上下文扩展" valuePropName="checked" tooltip="命中分块时附带其相邻分块，给模型更完整的上下文（small-to-big）。"><Switch /></Form.Item>
                <Form.Item name="content_safety_enabled" label="内容安全过滤" valuePropName="checked"><Switch /></Form.Item>
                <Form.Item name="semantic_cache_enabled" label="语义缓存" valuePropName="checked"><Switch /></Form.Item>
              </Space>
            </Card>
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
