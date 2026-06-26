import { useEffect, useState } from "react";
import { Button, Card, Form, Input, Switch, Tabs, Select, InputNumber, Space, Alert, ColorPicker, App as AntApp, Divider } from "antd";
import { channelApi } from "../api";
import { apiError } from "../api/client";
import { useAuth, isAdmin } from "../auth";

export function Channels() {
  return (
    <div>
      <div className="acs-page-title">渠道配置</div>
      <div className="acs-page-sub">配置 Web 对话窗口的品牌与防滥用、转人工通知方式，以及企业微信接入。</div>
      <Tabs
        items={[
          { key: "web", label: "Web 对话窗口", children: <WebTab /> },
          { key: "notify", label: "转人工通知", children: <NotifyTab /> },
          { key: "wechat", label: "企业微信", children: <WeChatTab /> },
        ]}
      />
    </div>
  );
}

function useChannelForm(get: () => Promise<any>, mapIn: (d: any) => any) {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const load = async () => form.setFieldsValue(mapIn(await get()));
  useEffect(() => { load(); }, []);
  return { form, loading, setLoading, load };
}

function WebTab() {
  const { user } = useAuth();
  const admin = isAdmin(user?.role);
  const { message } = AntApp.useApp();
  // channelKey + public base URL drive the embed snippet (both tenant-specific).
  const [embed, setEmbed] = useState({ key: "default", base: location.origin });
  const { form, loading, setLoading } = useChannelForm(channelApi.getWeb, (d) => {
    setEmbed({ key: d.key || "default", base: (d.app_base_url || location.origin).replace(/\/$/, "") });
    return {
      enabled: d.enabled,
      allowed_domains: d.allowed_domains,
      rate_limit_user_per_min: d.rate_limit_user_per_min,
      rate_limit_ip_per_min: d.rate_limit_ip_per_min,
      system_prompt_override: d.system_prompt_override,
      ...d.settings,
    };
  });

  const save = async () => {
    const v = await form.validateFields();
    setLoading(true);
    try {
      await channelApi.updateWeb({
        enabled: v.enabled,
        allowed_domains: v.allowed_domains,
        rate_limit_user_per_min: v.rate_limit_user_per_min,
        rate_limit_ip_per_min: v.rate_limit_ip_per_min,
        system_prompt_override: v.system_prompt_override,
        settings: {
          welcome_message: v.welcome_message, theme_color: typeof v.theme_color === "string" ? v.theme_color : v.theme_color?.toHexString?.(),
          logo_url: v.logo_url, brand_name: v.brand_name, placeholder: v.placeholder,
          default_theme: v.default_theme, show_powered_by: v.show_powered_by,
          image_understanding_enabled: v.image_understanding_enabled,
          file_upload_enabled: v.file_upload_enabled,
          suggested_questions: v.suggested_questions || [],
        },
      });
      message.success("已保存");
    } catch (e) { message.error(apiError(e)); } finally { setLoading(false); }
  };

  return (
    <Card>
      <Form form={form} layout="vertical" disabled={!admin} style={{ maxWidth: 720 }}>
        <Form.Item name="enabled" label="启用 Web 渠道" valuePropName="checked"><Switch /></Form.Item>
        <Divider orientation="left">品牌化</Divider>
        <Form.Item name="brand_name" label="品牌名称"><Input /></Form.Item>
        <Form.Item name="welcome_message" label="欢迎语"><Input.TextArea rows={2} /></Form.Item>
        <Form.Item name="suggested_questions" label="快捷问题（欢迎语下方可点选，引导高频问题）">
          <Select mode="tags" tokenSeparators={["\n"]} placeholder="输入后回车添加，如：怎么退货？" />
        </Form.Item>
        <Space size="large" wrap>
          <Form.Item name="theme_color" label="主题色"><ColorPicker showText /></Form.Item>
          <Form.Item name="default_theme" label="默认主题"><Select style={{ width: 120 }} options={[{ value: "light", label: "亮色" }, { value: "dark", label: "暗色" }]} /></Form.Item>
          <Form.Item name="show_powered_by" label="显示 Powered By" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="file_upload_enabled" label="允许上传文件" valuePropName="checked"><Switch /></Form.Item>
          <Form.Item name="image_understanding_enabled" label="AI 读图（多模态）" valuePropName="checked" tooltip="开启后把客户上传的图片交给 AI 理解；需所用模型/中转支持图片输入，关闭时图片仅供人工查看"><Switch /></Form.Item>
        </Space>
        <Form.Item name="logo_url" label="Logo URL"><Input placeholder="https://..." /></Form.Item>
        <Form.Item name="placeholder" label="输入框提示"><Input /></Form.Item>

        <Divider orientation="left">防滥用</Divider>
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message="域名白名单为空时允许所有来源（仅建议开发环境）。生产环境务必填写允许嵌入的域名。" />
        <Form.Item name="allowed_domains" label="允许嵌入的域名白名单">
          <Select mode="tags" placeholder="如 example.com 或 *.example.com" />
        </Form.Item>
        <Space size="large">
          <Form.Item name="rate_limit_user_per_min" label="单用户限流/分钟"><InputNumber min={0} placeholder="默认 20" /></Form.Item>
          <Form.Item name="rate_limit_ip_per_min" label="单 IP 限流/分钟"><InputNumber min={0} placeholder="默认 60" /></Form.Item>
        </Space>

        <Divider orientation="left">渠道人设（可选）</Divider>
        <Form.Item name="system_prompt_override" label="覆盖该渠道的 System Prompt"><Input.TextArea rows={4} placeholder="留空则用全局 AI 配置的 System Prompt" /></Form.Item>

        <Divider orientation="left">嵌入代码</Divider>
        <Alert type="info" message="把下面代码粘贴到目标网站，即可在右下角加载客服窗口（已绑定本租户的知识库与品牌）：" style={{ marginBottom: 8 }} />
        <Input.TextArea readOnly rows={5} value={`<script>\n  window.ACS_CONFIG = { channelKey: "${embed.key}" };\n</script>\n<script src="${embed.base}/embed/widget.js"></script>`} />
        <Alert type="info" message="或直接把这个独立对话页地址发给客户：" style={{ margin: "8px 0" }} />
        <Input readOnly value={`${embed.base}/chat?channel_key=${embed.key}`} />

        {admin && <div style={{ marginTop: 16 }}><Button type="primary" loading={loading} onClick={save}>保存</Button></div>}
      </Form>
    </Card>
  );
}

function NotifyTab() {
  const { user } = useAuth();
  const admin = isAdmin(user?.role);
  const { message } = AntApp.useApp();
  const { form, loading, setLoading } = useChannelForm(channelApi.getNotify, (d) => ({ enabled: d.enabled, ...d.settings }));
  const save = async () => {
    const v = await form.validateFields();
    setLoading(true);
    try { await channelApi.updateNotify(v); message.success("已保存"); }
    catch (e) { message.error(apiError(e)); } finally { setLoading(false); }
  };
  return (
    <Card>
      <Form form={form} layout="vertical" disabled={!admin} style={{ maxWidth: 640 }}>
        <Alert type="info" showIcon style={{ marginBottom: 16 }} message="转人工时通过以下方式通知运营者，至少配置一种。" />
        <Form.Item name="enabled" label="启用通知" valuePropName="checked"><Switch /></Form.Item>
        <Divider orientation="left">企业微信群机器人</Divider>
        <Form.Item name="wechat_webhook_url" label="Webhook URL"><Input placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." /></Form.Item>
        <Divider orientation="left">邮件（SMTP）</Divider>
        <Form.Item name="email_to" label="接收邮箱"><Input placeholder="ops@example.com" /></Form.Item>
        <Space size="large" wrap>
          <Form.Item name="smtp_host" label="SMTP Host"><Input /></Form.Item>
          <Form.Item name="smtp_port" label="端口"><InputNumber min={1} max={65535} /></Form.Item>
          <Form.Item name="smtp_ssl" label="SSL" valuePropName="checked"><Switch /></Form.Item>
        </Space>
        <Form.Item name="smtp_user" label="SMTP 用户"><Input /></Form.Item>
        <Form.Item name="smtp_password" label="SMTP 密码"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
        <Form.Item name="smtp_from" label="发件人"><Input /></Form.Item>
        <Divider orientation="left">给客户展示的联系方式</Divider>
        <Form.Item name="customer_contact" label="转人工时附带的联系方式"><Input.TextArea rows={2} /></Form.Item>
        {admin && <Button type="primary" loading={loading} onClick={save}>保存</Button>}
      </Form>
    </Card>
  );
}

function WeChatTab() {
  const { user } = useAuth();
  const admin = isAdmin(user?.role);
  const { message } = AntApp.useApp();
  const { form, loading, setLoading } = useChannelForm(channelApi.getWeChat, (d) => ({ enabled: d.enabled, ...d.settings, secret: "", secret_enc: undefined }));
  const save = async () => {
    const v = await form.validateFields();
    setLoading(true);
    try { await channelApi.updateWeChat(v); message.success("已保存"); }
    catch (e) { message.error(apiError(e)); } finally { setLoading(false); }
  };
  return (
    <Card>
      <Form form={form} layout="vertical" disabled={!admin} style={{ maxWidth: 640 }}>
        <Alert type="info" showIcon style={{ marginBottom: 16 }}
          message="配置企业微信自建应用的回调参数。回调 URL："
          description={<code>{location.origin}/api/wechat/callback</code>} />
        <Form.Item name="enabled" label="启用企业微信渠道" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="corp_id" label="CorpID"><Input /></Form.Item>
        <Form.Item name="agent_id" label="AgentID"><Input /></Form.Item>
        <Form.Item name="token" label="Token"><Input /></Form.Item>
        <Form.Item name="encoding_aes_key" label="EncodingAESKey"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
        <Form.Item name="secret" label="Secret"><Input.Password placeholder="留空表示不修改" autoComplete="new-password" /></Form.Item>
        <Form.Item name="system_prompt_override" label="渠道人设（可选）"><Input.TextArea rows={3} /></Form.Item>
        {admin && <Button type="primary" loading={loading} onClick={save}>保存</Button>}
      </Form>
    </Card>
  );
}
