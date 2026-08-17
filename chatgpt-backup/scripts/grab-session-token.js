/*
 * 手动获取凭证的辅助脚本。
 *
 * 用法:
 *   1. 在 ChatGPT 桌面应用（或浏览器）里打开开发者工具的 Console。
 *      - Windows/Linux 桌面应用: Ctrl+Shift+I；macOS: Cmd+Option+I
 *      - 如果桌面应用没开开发者工具，用浏览器登录同一个账号也一样。
 *   2. 把下面整段代码粘贴进去回车。
 *   3. 结果会打印出来并复制到剪贴板，然后执行:
 *        chatgpt-backup login --paste
 *      把内容粘贴进去，按 Ctrl-D 结束。
 *
 * 说明: access token 只有几小时有效期。想让定时备份长期免维护，请按脚本最后
 * 打印的提示，额外复制 __Secure-next-auth.session-token 这个 Cookie 的值。
 */

(async () => {
  const out = {};

  try {
    const response = await fetch("/api/auth/session", { credentials: "include" });
    const session = await response.json();
    if (session && session.accessToken) {
      out.access_token = session.accessToken;
      out.account = (session.user && session.user.email) || null;
      out.expires = session.expires || null;
    }
  } catch (error) {
    console.error("读取 /api/auth/session 失败:", error);
  }

  // cf_clearance 不是 httpOnly，能直接读到；它必须和当前 User-Agent 配对使用。
  const cf = document.cookie.split(";").map((s) => s.trim()).find((s) => s.startsWith("cf_clearance="));
  if (cf) out.cf_clearance = cf.slice("cf_clearance=".length);
  out.user_agent = navigator.userAgent;

  const text = JSON.stringify(out, null, 2);
  console.log(text);

  try {
    await navigator.clipboard.writeText(text);
    console.log("%c已复制到剪贴板，现在执行: chatgpt-backup login --paste", "color:#10a37f;font-weight:bold");
  } catch (error) {
    console.log("复制失败，请手动选中上面的 JSON 复制。");
  }

  console.log(
    "%c想让备份长期免维护（不用每几小时重新登录）:",
    "color:#d97706;font-weight:bold"
  );
  console.log(
    "  开发者工具 → Application(应用) → Storage → Cookies → https://chatgpt.com\n" +
      "  找到 __Secure-next-auth.session-token，复制它的 Value，然后执行:\n" +
      "  chatgpt-backup login --session-token '<粘贴这里>'"
  );
})();
