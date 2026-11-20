#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const usage = `W-BOT Lab — Windows local group analytics

Usage:
  wbot-lab --data-dir ABSOLUTE_PATH [--python PATH] [--group NAME --wechat-dir ABSOLUTE_PATH] [--auto] [--browser] [--port PORT]
  wbot-lab mcp --data-dir ABSOLUTE_PATH [--python PATH]

--data-dir    Required writable location for analysis DB, temporary files and reports.
--python      Existing Python 3.10+ executable; alternatively set WBOT_PYTHON.
--group       Exact group name the owner authorizes this process to read.
--wechat-dir  The absolute path containing WeChat account directories.
--auto        Enable daily updates on first run; requires --group and --wechat-dir.
--browser     Open the local interface in an isolated browser profile.
--port        Localhost port for the dashboard (default 8765).

Data stays local unless an MCP client forwards results elsewhere. Image contents and
semantic AI summaries are not provided. See README.md for dependencies and limitations.
`;

function fail(message) {
  process.stderr.write(message + '\n\n' + usage);
  process.exitCode = 1;
}

function main(argv) {
  if (argv.includes('--help') || argv.includes('-h')) {
    process.stdout.write(usage);
    return;
  }
  let mode = 'web';
  if (argv[0] === 'mcp') { mode = 'mcp'; argv = argv.slice(1); }
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === '--auto' || key === '--browser') { args[key] = true; continue; }
    if (!['--data-dir', '--python', '--group', '--wechat-dir', '--port'].includes(key) || i + 1 >= argv.length) {
      return fail('Unknown or incomplete option: ' + key);
    }
    if (argv[i + 1].startsWith('--')) return fail('Missing value for ' + key);
    args[key] = argv[++i];
  }
  const data = args['--data-dir'] || process.env.WBOT_DATA_DIR;
  if (!data || !path.isAbsolute(data)) return fail('--data-dir must be an absolute path.');
  const root = path.resolve(__dirname, '..');
  const relativeData = path.relative(root, path.resolve(data));
  if (!relativeData || (!relativeData.startsWith('..' + path.sep) && relativeData !== '..' && !path.isAbsolute(relativeData))) {
    return fail('Choose a data directory outside the package source directory.');
  }
  const group = args['--group'] || process.env.WBOT_GROUP || '';
  const wechat = args['--wechat-dir'] || process.env.WBOT_WECHAT_DIR || '';
  if (wechat && !path.isAbsolute(wechat)) return fail('--wechat-dir must be an absolute path.');
  if (args['--auto'] && (!group || !wechat)) return fail('--auto requires --group and --wechat-dir.');
  if (args['--port'] && (!/^\d+$/.test(args['--port']) || Number(args['--port']) < 1 || Number(args['--port']) > 65535)) return fail('--port must be an integer from 1 to 65535.');
  if (mode === 'mcp' && (args['--auto'] || args['--browser'] || args['--port'])) return fail('mcp mode does not accept --auto, --browser, or --port.');

  const runtime = path.join(path.resolve(data), 'runtime');
  const temp = path.join(runtime, 'tmp');
  const dirs = { TMP:temp, TEMP:temp, TMPDIR:temp, USERPROFILE:path.join(runtime,'profile'),
                 APPDATA:path.join(runtime,'appdata'), LOCALAPPDATA:path.join(runtime,'localappdata') };
  try { for (const dir of Object.values(dirs)) fs.mkdirSync(dir, {recursive:true}); }
  catch (error) { return fail('Cannot prepare the data directory: ' + error.message); }
  const env = {...process.env, ...dirs, WBOT_DATA_DIR:path.resolve(data), WBOT_GROUP:group,
               WBOT_WECHAT_DIR:wechat, PYTHONDONTWRITEBYTECODE:'1', PYTHONIOENCODING:'utf-8'};
  if (args['--auto']) env.WBOT_AUTO_REPORTS = '1';
  if (args['--port']) env.WBOT_PORT = args['--port'];
  const python = args['--python'] || process.env.WBOT_PYTHON || 'python';
  const script = path.join(root, mode === 'mcp' ? 'mcp_server.py' : 'local_app.py');
  const childArgs = ['-B', '-u', script];
  if (mode === 'web' && args['--browser']) childArgs.push('--browser');
  const child = spawn(python, childArgs, {stdio:'inherit', cwd:root, env, windowsHide:true});
  child.on('error', error => { process.stderr.write('Unable to start Python: ' + error.message + '\n'); process.exitCode = 1; });
  child.on('exit', (code, signal) => { process.exitCode = code === null ? (signal ? 1 : 0) : code; });
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => { if (!child.killed) child.kill(signal); });
  }
}

main(process.argv.slice(2));
