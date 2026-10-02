const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('tiku_agent/demo_web/demo.js', 'utf8');
const start = source.indexOf('class UserVisibleError');
const end = source.indexOf('let history =', start);
const context = vm.createContext({
  normalizeRecoveryActions: value => value,
  protocolRecoveryAction: value => value,
});
vm.runInContext(source.slice(source.indexOf('function protocolFields('), source.indexOf('function protocolRecoveryAction(')), context);
vm.runInContext(source.slice(start, end) + `
  globalThis.display = userFacingErrorText;
  globalThis.expose = userFacingErrorMessage;
  globalThis.makeError = value => new UserVisibleError(value);
`, context);
const streamStart = source.indexOf('function streamedError(');
const streamEnd = source.indexOf('async function request(', streamStart);
vm.runInContext(source.slice(streamStart, streamEnd), context);
for (const raw of ['Failed to fetch', 'NetworkError when attempting to fetch resource.',
  'signal is aborted without reason', 'The operation was aborted.', 'Unexpected token <',
  'An unknown provider failure', '处理失败：TypeError: Cannot read properties of undefined']) {
  assert.match(context.display(raw), /[\u3400-\u9fff]/);
  assert.notEqual(context.display(raw), raw);
  assert.match(context.makeError(raw).message, /[\u3400-\u9fff]/);
}
assert.equal(context.display('登录已失效，请重新登录。'), '登录已失效，请重新登录。');
assert.equal(context.display('请求失败（HTTP 503），请稍后重试。'), '请求失败（HTTP 503），请稍后重试。');
assert.equal(context.expose({ publicMessage: 'Internal Server Error' }, '选题失败，请重新选择。'), '选题失败，请重新选择。');
assert.equal(context.expose(new Error('internal details'), '裁剪校验失败，请重试。'), '裁剪校验失败，请重试。');
assert.equal(context.display('', 'English fallback'), '暂时无法完成操作，请稍后重试。');
assert.match(context.streamedError({ message: 'Internal Server Error', status: 'ERROR', action: 'retry_request' }).message, /[\u3400-\u9fff]/);
assert.equal(context.streamedError({ message: '请裁剪单个结构图后重试。', status: 'ERROR' }).message, '请裁剪单个结构图后重试。');
console.log('Chinese error display checks passed');
