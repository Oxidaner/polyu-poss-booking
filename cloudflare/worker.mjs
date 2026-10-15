export default {
  async scheduled(_event, env) {
    const response = await fetch(
      "https://api.github.com/repos/Oxidaner/polyu-poss-booking/actions/workflows/monitor.yml/dispatches",
      {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
          "Content-Type": "application/json",
          "User-Agent": "polyu-poss-booking-dispatcher",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );
    if (!response.ok) {
      throw new Error(`GitHub workflow dispatch failed (HTTP ${response.status})`);
    }
    console.log(`GitHub workflow dispatch accepted (HTTP ${response.status})`);
  },
};
