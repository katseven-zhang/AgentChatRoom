const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.js'), 'utf8');
const start = source.indexOf('const PROJECT_CREDENTIAL_BUNDLE_PREFIX');
const functionStart = source.indexOf('function issuedHttpConfig(', start);
const end = source.indexOf('\n}', functionStart) + 2;
const context = {
  TextEncoder,
  TextDecoder,
  btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
  atob: (value) => Buffer.from(value, 'base64').toString('binary'),
  crypto: {randomUUID: () => '11111111-2222-4333-8444-555555555555'},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

const member = {
  name: 'Chosen Agent',
  metadata: {software_key: 'chosen', client: 'chosen-client'},
};
const profile = {
  format: 'json',
  streamable_http_config_text: JSON.stringify({mcpServers: {agentchatroom: {
    url: '/mcp',
    headers: {Authorization: 'Bearer <paste-issued-project-credential-bundle>'},
  }}}),
  onboarding_modes: {
    first_setup: {http: 'Install this config:\nPLACEHOLDER'},
    add_project: {http: 'Join Project B with room_bootstrap(project_name="Project B")'},
  },
};
profile.onboarding_modes.first_setup.http = profile.onboarding_modes.first_setup.http.replace(
  'PLACEHOLDER', profile.streamable_http_config_text,
);
const entries = [
  {name: 'Project A', token: 'acr.credential-a.secret-a'},
  {name: 'Project B', token: 'acr.credential-b.secret-b'},
];
const original = JSON.stringify(profile);
const memberIdentity = context.softwareIdentityForMember(member);
const result = JSON.parse(context.issuedHttpConfig(profile, entries, memberIdentity));
const authorization = result.mcpServers.agentchatroom.headers.Authorization;
assert.match(authorization, /^Bearer acrb\.v1\./);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.decodeProjectCredentialBundle(authorization.slice(7)))),
  entries,
);
assert.equal(result.mcpServers.agentchatroom.headers['X-AgentChatRoom-Software-Key'], 'chosen');
assert.equal(JSON.stringify(profile), original);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.parseExistingProjectCredentials(JSON.stringify(result)))),
  entries,
);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.parseExistingSoftwareIdentity(JSON.stringify(result)))),
  {softwareKey: 'chosen', softwareName: 'Chosen Agent', softwareClient: 'chosen-client'},
);
assert.equal(
  context.sameSoftwareIdentity(
    context.parseExistingSoftwareIdentity(JSON.stringify(result)),
    context.softwareIdentityForMember(member),
  ),
  true,
);
const prompt = context.issuedHttpPrompt(profile, {
  mode: 'add_project',
  projectName: 'Project B',
  projectCredentials: entries,
  softwareIdentity: memberIdentity,
});
assert.match(prompt, /目标 Project：Project B/);
assert.match(prompt, /本工作区固定 bootstrap 参数：project_name="Project B"/);
assert.match(prompt, /project_name_1="Project A"/);
assert.match(prompt, /project_token_1="acr\.credential-a\.secret-a"/);
assert.match(prompt, /project_name_2="Project B"/);
assert.match(prompt, /project_token_2="acr\.credential-b\.secret-b"/);
assert.match(prompt, /"agentchatroom"/);
assert.match(prompt, /Bearer acrb\.v1\./);
assert.match(prompt, /room_bootstrap\(project_name="Project B"\)/);
assert.match(prompt, /不要命名为 agentchatroom-stdio/);
const firstPrompt = context.issuedHttpPrompt(profile, {
  mode: 'first_setup',
  projectName: 'Project A',
  projectCredentials: [entries[0]],
  softwareIdentity: memberIdentity,
});
assert.match(firstPrompt, /接入场景：首次配置软件/);
assert.match(firstPrompt, /目标 Project：Project A/);
assert.match(firstPrompt, /project_name_1="Project A"/);
assert.match(firstPrompt, /project_token_1="acr\.credential-a\.secret-a"/);
assert.doesNotMatch(firstPrompt, /paste-issued-project-credential-bundle/);
const incrementalPrompt = context.incrementalHttpPrompt(profile, {
  mode: 'add_project',
  projectName: 'Project B',
  projectCredentials: [entries[1]],
  incremental: true,
});
assert.match(incrementalPrompt, /接入场景：已配置软件，加入本项目（增量合并）/);
assert.match(incrementalPrompt, /目标 Project：Project B/);
assert.match(incrementalPrompt, /本工作区固定 bootstrap 参数：project_name="Project B"/);
assert.match(incrementalPrompt, /MCP Server 标准名称：agentchatroom/);
assert.match(incrementalPrompt, /project_name_1="Project B"/);
assert.match(incrementalPrompt, /project_token_1="acr\.credential-b\.secret-b"/);
assert.doesNotMatch(incrementalPrompt, /project_name_2/);
assert.doesNotMatch(incrementalPrompt, /credential-a/);
assert.match(incrementalPrompt, /不要新建第二个 agentchatroom/);
assert.match(incrementalPrompt, /软件身份三字段/);
assert.match(incrementalPrompt, /全部旧 Project 凭据/);
assert.match(incrementalPrompt, /写回同一个 agentchatroom 条目/);
assert.match(incrementalPrompt, /room_bootstrap\(project_name="Project B"\)/);
assert.match(incrementalPrompt, /acrb\.v1/);
assert.doesNotMatch(incrementalPrompt, /Bearer acrb\.v1\.[A-Za-z0-9_-]/);
assert.match(incrementalPrompt, /现值为单个 `Bearer acr\.\*` Token/);
assert.match(incrementalPrompt, /零参数 `room_bootstrap\(\)`/);
assert.match(incrementalPrompt, /旧Project名称=旧Token/);
assert.match(incrementalPrompt, /高级 · 故障恢复/);
assert.match(incrementalPrompt, /无法读取或找不到现有 agentchatroom 配置/);
assert.match(incrementalPrompt, /不要把上述 Token 发布到 Room、日志或仓库/);
assert.match(incrementalPrompt, /本次 Token 未关联成员/);
assert.match(incrementalPrompt, /第一次成功连接时会按该身份自动登记成员/);
const linkedIncrementalPrompt = context.incrementalHttpPrompt(profile, {
  mode: 'add_project',
  projectName: 'Project B',
  projectCredentials: [entries[1]],
  incremental: true,
  member,
  softwareIdentity: memberIdentity,
});
assert.match(linkedIncrementalPrompt, /本次 Token 已关联成员 "Chosen Agent"/);
assert.match(linkedIncrementalPrompt, /X-AgentChatRoom-Software-Key="chosen"/);
assert.match(linkedIncrementalPrompt, /现有配置可以没有这三个 Header/);
assert.match(linkedIncrementalPrompt, /若已经配置 Header/);
assert.match(linkedIncrementalPrompt, /其他已关联 Token 也必须属于同一软件身份/);
const replaced = context.mergeProjectCredentials(entries, {
  name: 'Project B',
  token: 'acr.credential-b2.secret-b2',
});
assert.equal(replaced.length, 2);
assert.equal(replaced[1].token, 'acr.credential-b2.secret-b2');

for (const header of ['http_headers', 'headers']) {
  const template = [
    '[mcp_servers.agentchatroom]',
    'url = "/mcp"',
    'bearer_token_env_var = "AGENTCHATROOM_AGENT_TOKEN"',
    `[mcp_servers.agentchatroom.${header}]`,
    'X-AgentChatRoom-Software-Key = "old"',
    '',
  ].join('\n');
  const config = context.issuedHttpConfig(
    {format: 'toml', streamable_http_config_text: template},
    entries,
    memberIdentity,
  );
  assert.match(config, /Authorization = "Bearer acrb\.v1\./);
  assert.doesNotMatch(config, /bearer_token_env_var/);
  assert.match(config, /Software-Key = "chosen"/);
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.parseExistingProjectCredentials(config))),
    entries,
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.parseExistingSoftwareIdentity(config))),
    {softwareKey: 'chosen', softwareName: 'Chosen Agent', softwareClient: 'chosen-client'},
  );
}

const genericProfile = {
  label: '通用（标准 MCP）',
  software_key: '<stable-software-key>',
  software_name: '<Software name>',
  software_client: '<software-client-code>',
};
// 通用 profile 的 label 只是接入格式标签：既不能作为软件身份名称，也不能兜底成名称。
assert.equal(context.accessFormatProfileLabel(genericProfile), '通用（标准 MCP）');
assert.throws(() => context.createSoftwareIdentityForProfile(genericProfile), /显示名称/);
assert.throws(() => context.createSoftwareIdentityForProfile(genericProfile, '   '), /显示名称/);
assert.throws(
  () => context.createSoftwareIdentityForProfile(genericProfile, '通用（标准 MCP）'),
  /接入格式标签/,
);
const genericIdentity = context.createSoftwareIdentityForProfile(genericProfile, 'Hermes');
assert.deepEqual(
  JSON.parse(JSON.stringify(genericIdentity)),
  {
    softwareKey: 'standard-mcp-11111111-2222-4333-8444-555555555555',
    softwareName: 'Hermes',
    softwareClient: 'standard-mcp',
  },
);
const cleanDatabaseConfig = JSON.parse(context.issuedHttpConfig(
  profile,
  [entries[0]],
  genericIdentity,
));
assert.equal(
  cleanDatabaseConfig.mcpServers.agentchatroom.headers['X-AgentChatRoom-Software-Key'],
  genericIdentity.softwareKey,
);
// ASCII 名称在配置里保持明文。
assert.equal(
  cleanDatabaseConfig.mcpServers.agentchatroom.headers['X-AgentChatRoom-Software-Name'],
  'Hermes',
);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.parseExistingSoftwareIdentity(JSON.stringify(cleanDatabaseConfig)))),
  JSON.parse(JSON.stringify(genericIdentity)),
);
// 中文名称只在线路层编码，解析与展示必须还原原文。
const chineseIdentity = context.createSoftwareIdentityForProfile(genericProfile, '猎鹰');
const chineseConfig = JSON.parse(context.issuedHttpConfig(profile, [entries[0]], chineseIdentity));
const encodedChineseName = chineseConfig.mcpServers.agentchatroom.headers['X-AgentChatRoom-Software-Name'];
assert.match(encodedChineseName, /^acr-utf8\.v1\.[A-Za-z0-9_-]+$/);
assert.equal(context.decodeHttpIdentityHeaderValue(encodedChineseName), '猎鹰');
assert.deepEqual(
  JSON.parse(JSON.stringify(context.parseExistingSoftwareIdentity(JSON.stringify(chineseConfig)))),
  JSON.parse(JSON.stringify(chineseIdentity)),
);
// 具名客户端 profile 自带确定身份，其 label 不是格式标签，可直接作为显示名称。
const namedProfile = {
  label: 'WorkBuddy',
  software_key: 'workbuddy',
  software_name: 'WorkBuddy',
  software_client: 'workbuddy',
};
assert.equal(context.accessFormatProfileLabel(namedProfile), '');
assert.equal(
  context.createSoftwareIdentityForProfile(namedProfile, 'WorkBuddy').softwareName,
  'WorkBuddy',
);

assert.throws(
  () => context.normalizedSoftwareIdentity({softwareKey: 'only-key'}),
  /软件身份字段不完整/,
);
assert.equal(
  context.sameSoftwareIdentity(
    {softwareKey: 'chosen', softwareName: 'Chosen Agent', softwareClient: 'chosen-client'},
    {softwareKey: 'other', softwareName: 'Chosen Agent', softwareClient: 'chosen-client'},
  ),
  false,
);

console.log('HTTP multi-project config generation passed');
