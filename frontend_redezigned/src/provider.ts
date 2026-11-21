import { createApiProvider } from './data';
import { createMockProvider } from './mock';

const passportPath = import.meta.env.VITE_PROJECT_PASSPORT_PATH?.trim() || 'export/project-passport.pdf';
const runId = import.meta.env.VITE_PROJECT_RUN_ID?.trim();
const passport = runId && /^[\w/.-]+\.pdf$/.test(passportPath)
  ? { path: passportPath, runId, generatedAtHeader: import.meta.env.VITE_PROJECT_PASSPORT_GENERATED_AT_HEADER?.trim() }
  : undefined;

export const provider = import.meta.env.DEV && import.meta.env.VITE_DATA_SOURCE === 'mock'
  ? createMockProvider({ base: `${import.meta.env.BASE_URL}demo` })
  : createApiProvider({ base: import.meta.env.VITE_API_BASE || '/api', csvBase: import.meta.env.VITE_CSV_ARTIFACT_BASE, passport });
