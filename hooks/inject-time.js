// opencode plugin: 每条用户消息注入当前系统时间（fail-open，出错不影响对话）
// 时段由代码算好给出：只给 HH:MM:SS 时模型会自己换算，曾在中午 11:45 说出"今晚"
const PERIODS = [
  [4, "凌晨"],
  [8, "早上"],
  [10, "上午"],
  [13, "中午"],
  [17, "下午"],
  [22, "晚上"],
];

const periodOf = (hour) => PERIODS.find(([end]) => hour <= end)?.[1] ?? "深夜";

export const InjectTime = async () => {
  return {
    "chat.message": async (input, output) => {
      try {
        const now = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        const date = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
        const weekday = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][now.getDay()];
        const stamp = `${date} ${pad(now.getHours())}:${pad(now.getMinutes())} ${weekday}（${periodOf(now.getHours())}）`;
        output.parts.push({
          id: `prt_time${now.getTime().toString(36)}`,
          sessionID: input.sessionID,
          messageID: output.message.id,
          type: "text",
          text: `<system-context>当前系统时间: ${stamp}\n时段词（现在/今天/今晚/明早/周末）一律以此为准；括号里的时段直接用，不要自己换算，也不要沿用会话历史里更早的时间。</system-context>`,
          synthetic: true,
        });
      } catch {
        // fail-open
      }
    },
  };
};
