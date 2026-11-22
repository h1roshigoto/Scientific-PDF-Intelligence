import { existsSync } from 'node:fs';
import { cp } from 'node:fs/promises';
import { resolve } from 'node:path';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const rootPassport = resolve(__dirname, '../passport.md');
  const localPassport = resolve(__dirname, 'src/data/passport.md');
  const proxy = {
    '/api': {
      target: env.SPI_BACKEND_URL || 'http://127.0.0.1:8000',
      // FastAPI validates the original browser Host and Origin itself.
      changeOrigin: false,
      xfwd: false,
      rewrite: (path: string) => path.replace(/^\/api(?=\/|$)/, ''),
    },
  };
  return {
    build: { copyPublicDir: false },
    plugins: [{
      name: 'copy-runtime-assets',
      apply: 'build',
      async writeBundle({ dir }) {
        if (dir) await cp(resolve(__dirname, 'public/assets'), resolve(dir, 'assets'), { recursive: true });
      },
    }],
    resolve: {
      alias: [
        { find: /^@passport(?=\?|$)/, replacement: existsSync(rootPassport) ? rootPassport : localPassport },
      ],
    },
    server: { proxy, fs: { allow: [__dirname, rootPassport] } },
    preview: { proxy },
  };
});
