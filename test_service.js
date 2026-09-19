"use strict";

const { spawnSync } = require("node:child_process");

// 先运行全部 test_*.py，再保留既有 service_contract 契约
const commands = [
  ["python3", ["-m", "unittest", "discover", "-v", "-p", "test_*.py"]],
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
];

for (const [cmd, args] of commands) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
