import type { CSSProperties } from "react";
import {
  RiAddLine,
  RiArrowDownSLine,
  RiArrowRightSLine,
  RiAttachment2,
  RiBookOpenLine,
  RiBuilding4Line,
  RiChatHistoryLine,
  RiChatQuoteLine,
  RiCpuLine,
  RiCustomerService2Line,
  RiDashboard3Line,
  RiDownload2Line,
  RiEditLine,
  RiEqualizer2Line,
  RiExportLine,
  RiExternalLinkLine,
  RiFlaskLine,
  RiHistoryLine,
  RiLinksLine,
  RiLoginBoxLine,
  RiLogoutBoxRLine,
  RiLoopLeftLine,
  RiMenuFoldLine,
  RiMenuUnfoldLine,
  RiMoonLine,
  RiNodeTree,
  RiPlug2Line,
  RiQuestionAnswerLine,
  RiRefreshLine,
  RiRobot2Line,
  RiSearchLine,
  RiShieldUserLine,
  RiSunLine,
  RiTicket2Line,
  RiToolsLine,
  RiUpload2Line,
  RiUserLine,
  type RemixiconComponentType,
} from "@remixicon/react";

/** 后台统一图标集（Remix Icon 线性款）。页面只从这里按「语义名」取图标，
 *  不直接依赖图标库 —— 换库或换某个图标时只改这一个文件。
 *
 *  带上 antd 的 `anticon` 类名，是为了沿用 Button / Menu / Tag / Input 里
 *  「图标 + 文字」的间距与折叠态规则，不必逐个组件补样式。 */
type IconProps = { className?: string; style?: CSSProperties; size?: number | string };

const wrap = (Glyph: RemixiconComponentType) =>
  function Icon({ className, size = "1.15em", ...rest }: IconProps) {
    return <Glyph aria-hidden size={size} className={className ? `anticon acs-icon ${className}` : "anticon acs-icon"} {...rest} />;
  };

// 导航
export const IconOverview = wrap(RiDashboard3Line);
export const IconConversations = wrap(RiChatHistoryLine);
export const IconTicket = wrap(RiTicket2Line);
export const IconAgentDesk = wrap(RiCustomerService2Line);
export const IconKnowledge = wrap(RiBookOpenLine);
export const IconCanned = wrap(RiChatQuoteLine);
export const IconModel = wrap(RiCpuLine);
export const IconChannel = wrap(RiPlug2Line);
export const IconAccounts = wrap(RiShieldUserLine);
export const IconTenant = wrap(RiBuilding4Line);

// 外壳
export const IconMenuFold = wrap(RiMenuFoldLine);
export const IconMenuUnfold = wrap(RiMenuUnfoldLine);
export const IconSun = wrap(RiSunLine);
export const IconMoon = wrap(RiMoonLine);
export const IconLogout = wrap(RiLogoutBoxRLine);
export const IconCaretDown = wrap(RiArrowDownSLine);
export const IconChevronRight = wrap(RiArrowRightSLine);
export const IconExternal = wrap(RiExternalLinkLine);

// 操作
export const IconAdd = wrap(RiAddLine);
export const IconSearch = wrap(RiSearchLine);
export const IconEdit = wrap(RiEditLine);
export const IconRefresh = wrap(RiRefreshLine);
export const IconUpload = wrap(RiUpload2Line);
export const IconDownload = wrap(RiDownload2Line);
export const IconExport = wrap(RiExportLine);
export const IconHistory = wrap(RiHistoryLine);
export const IconEnter = wrap(RiLoginBoxLine);

// 知识库 / 模型
export const IconChunks = wrap(RiNodeTree);
export const IconTestSearch = wrap(RiFlaskLine);
export const IconReembed = wrap(RiLoopLeftLine);
export const IconPhrasings = wrap(RiQuestionAnswerLine);
export const IconConnect = wrap(RiLinksLine);
export const IconCalibrate = wrap(RiEqualizer2Line);

// 对话详情
export const IconVisitor = wrap(RiUserLine);
export const IconBot = wrap(RiRobot2Line);
export const IconTool = wrap(RiToolsLine);
export const IconAttachment = wrap(RiAttachment2);
