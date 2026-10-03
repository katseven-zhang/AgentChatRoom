const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// The handoff prompt and the config-with-real-credentials are produced by the
// server (integrations.build_issued_credential_prompt / issued_http_config_text)
// and asserted in tests/test_onboarding_prompt_accuracy.py. What remains here is
// the browser's own half: reading a credential bundle and a software identity
// back out of a config, and refusing an identity that is only a format label.
const source = fs.readFileSync(path.join(__dirname, '../src/agentchatroom/web/app.js'), 'utf8');
const start = source.indexOf('const PROJECT_CREDENTIAL_BUNDLE_PREFIX');
const end = source.indexOf('function handleError(');
const context = {
  TextEncoder,
  TextDecoder,
  btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
  atob: (value) => Buffer.from(value, 'base64').toString('binary'),
  crypto: {randomUUID: () => '11111111-2222-4333-8444-555555555555'},
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

// The prompt generators must not come back as a second browser-side copy.
assert.equal(context.issuedHttpPrompt, undefined);
assert.equal(context.incrementalHttpPrompt, undefined);
assert.equal(context.issuedHttpConfig, undefined);

const member = {
  name: 'Chosen Agent',
  metadata: {software_key: 'chosen', client: 'chosen-client'},
};
const entries = [
  {name: 'Project A', token: 'acr.credential-a.secret-a'},
  {name: 'Project B', token: 'acr.credential-b.secret-b'},
];
const memberIdentity = context.softwareIdentityForMember(member);
assert.equal(memberIdentity.softwareKey, 'chosen');
assert.equal(memberIdentity.softwareName, 'Chosen Agent');
assert.equal(memberIdentity.softwareClient, 'chosen-client');

const encoded = context.encodeProjectCredentialBundle(entries);
assert.match(encoded, /^acrb\.v1\./);
assert.deepEqual(
  JSON.parse(JSON.stringify(context.decodeProjectCredentialBundle(encoded))),
  entries,
);
assert.equal(context.encodeProjectCredentialBundle(entries), encoded);
assert.throws(() => context.encodeProjectCredentialBundle([]), /1 到 32/);
assert.throws(
  () => context.encodeProjectCredentialBundle([
    {name: 'Same', token: 'acr.one'},
    {name: 'same', token: 'acr.two'},
  ]),
  /必须唯一/,
);
assert.throws(
  () => context.encodeProjectCredentialBundle([{name: 'X', token: 'opaque'}]),
  /格式无效/,
);
assert.throws(
  () => context.parseExistingProjectCredentials(
    JSON.stringify({mcpServers: {agentchatroom: {headers: {Authorization: 'Bearer opaque'}}}}),
  ),
  /凭据包/,
);
assert.throws(
  () => context.parseExistingProjectCredentials(
    JSON.stringify({
      mcpServers: {agentchatroom: {headers: {Authorization: `Bearer ${encoded}extra`}}},
    }),
  ),
  /凭据包/,
);

// Identity survives a round trip through both config syntaxes, including the
// non-ASCII wire encoding.
const jsonConfig = JSON.stringify({
  mcpServers: {agentchatroom: {headers: {
    'X-AgentChatRoom-Software-Key': 'chosen',
    'X-AgentChatRoom-Software-Name': 'Chosen Agent',
    'X-AgentChatRoom-Software-Client': 'chosen-client',
  }}},
});
assert.deepEqual(
  JSON.parse(JSON.stringify(context.parseExistingSoftwareIdentity(jsonConfig))),
  {softwareKey: 'chosen', softwareName: 'Chosen Agent', softwareClient: 'chosen-client'},
);
const chineseIdentity = context.createSoftwareIdentityForProfile(
  {
    label: '通用（标准 MCP）',
    software_key: '<stable-software-key>',
    software_name: '<Software name>',
    software_client: '<software-client-code>',
  },
  '猎鹰',
);
const encodedName = context.encodeHttpIdentityHeaderValue(chineseIdentity.softwareName);
assert.match(encodedName, /^acr-utf8\.v1\.[A-Za-z0-9_-]+$/);
assert.equal(context.decodeHttpIdentityHeaderValue(encodedName), '猎鹰');
assert.equal(context.encodeHttpIdentityHeaderValue('Hermes'), 'Hermes');

for (const header of ['http_headers', 'headers']) {
  const toml = [
    '[mcp_servers.agentchatroom]',
    'url = "/mcp"',
    `[mcp_servers.agentchatroom.${header}]`,
    'X-AgentChatRoom-Software-Key = "chosen"',
    'X-AgentChatRoom-Software-Name = "Chosen Agent"',
    'X-AgentChatRoom-Software-Client = "chosen-client"',
    '',
  ].join('\n');
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.parseExistingSoftwareIdentity(toml))),
    {softwareKey: 'chosen', softwareName: 'Chosen Agent', softwareClient: 'chosen-client'},
  );
  const tomlWithBundle = `${toml}Authorization = "Bearer ${encoded}"\n`;
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.parseExistingProjectCredentials(tomlWithBundle))),
    entries,
  );
}

// A format label is not a display name.
const genericProfile = {
  label: '通用（标准 MCP）',
  software_key: '<stable-software-key>',
  software_name: '<Software name>',
  software_client: '<software-client-code>',
};
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

// A named client carries a determined identity; its label is not a format label.
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
assert.equal(
  context.sameSoftwareIdentity(memberIdentity, {
    softwareKey: 'chosen', softwareName: 'Chosen Agent', softwareClient: 'chosen-client',
  }),
  true,
);

const replaced = context.mergeProjectCredentials(entries, {
  name: 'Project B',
  token: 'acr.credential-b2.secret-b2',
});
assert.equal(replaced.length, 2);
assert.equal(replaced[1].token, 'acr.credential-b2.secret-b2');
assert.equal(replaced[0].token, 'acr.credential-a.secret-a');

console.log('HTTP credential and identity round trips passed');
