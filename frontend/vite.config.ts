import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxy = {
    '/api': {
      target: env.SPI_BACKEND_URL || 'http://127.0.0.1:8000',
      rewrite: (path: string) => path.replace(/^\/api(?=\/|$)/, ''),
    },
  };
  return { server: { proxy }, preview: { proxy } };
});
