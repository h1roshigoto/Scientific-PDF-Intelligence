import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/unit/**/*.test.ts', 'tests/integration/**/*.test.ts'],
    reporters: ['default', 'junit'],
    outputFile: { junit: 'test-results/vitest.xml' },
  },
});
