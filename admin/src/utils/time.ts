import dayjs from "dayjs";

// Backend sends UTC ISO timestamps (e.g. 2026-06-24T06:02:09+00:00). dayjs parses the
// offset and renders in the viewer's local timezone — fixing the "times look 8h off /
// date wrong near midnight" bug from naively truncating the ISO string.

/** Full local datetime, e.g. 2026-06-24 14:02:09. Empty → "—". */
export const fmtTime = (s?: string | null) => (s ? dayjs(s).format("YYYY-MM-DD HH:mm:ss") : "—");

/** Short local datetime for compact lists, e.g. 06-24 14:02. Empty → "". */
export const fmtShort = (s?: string | null) => (s ? dayjs(s).format("MM-DD HH:mm") : "");
