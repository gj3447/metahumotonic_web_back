const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 12)) {
  console.error(`Node ${process.versions.node} is unsupported. Use scripts/with-node.sh npm <command> (pinned in .node-version).`);
  process.exit(78);
}
console.log(`Runtime accepted: Node ${process.versions.node}`);
