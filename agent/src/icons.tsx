import type { CSSProperties } from "react";
import {
  RiArrowDownSLine,
  RiArrowLeftSLine,
  RiAttachment2,
  RiChat3Line,
  RiChatQuoteLine,
  RiEmotionLine,
  RiInboxLine,
  RiMoonLine,
  RiNotification3Line,
  RiNotificationOffLine,
  RiSunLine,
  type RemixiconComponentType,
} from "@remixicon/react";

/** 坐席站图标集：与管理后台同一套 Remix Icon 线性图标，页面只按语义名取用。
 *  按需引入（每个图标约 0.5KB），不影响移动端体积。 */
type IconProps = { className?: string; style?: CSSProperties; size?: number | string };

const wrap = (Glyph: RemixiconComponentType) =>
  function Icon({ className, size = 18, ...rest }: IconProps) {
    return <Glyph aria-hidden size={size} className={className ? `icon ${className}` : "icon"} {...rest} />;
  };

export const IconBack = wrap(RiArrowLeftSLine);
export const IconDown = wrap(RiArrowDownSLine);
export const IconSun = wrap(RiSunLine);
export const IconMoon = wrap(RiMoonLine);
export const IconBell = wrap(RiNotification3Line);
export const IconBellOff = wrap(RiNotificationOffLine);
export const IconCanned = wrap(RiChatQuoteLine);
export const IconAttach = wrap(RiAttachment2);
export const IconEmoji = wrap(RiEmotionLine);
export const IconInbox = wrap(RiInboxLine);
export const IconChat = wrap(RiChat3Line);
