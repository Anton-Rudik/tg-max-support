const path = require('node:path');

module.exports = {
  apps: [
    {
      name: 'tg-max-support',
      cwd: __dirname,
      script: path.join(__dirname, '.venv', 'bin', 'uvicorn'),
      args: `app.main:app --host 127.0.0.1 --port ${process.env.APP_PORT || 8000} --workers 1`,
      interpreter: 'none',
      exec_mode: 'fork',
      instances: 1,
      autorestart: true,
      restart_delay: 3000,
      time: true,
      env: { PYTHONUNBUFFERED: '1' },
    },
  ],
};
