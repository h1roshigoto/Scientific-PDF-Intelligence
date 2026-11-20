import { createApiProvider } from './data';
import { createMockProvider } from './mock';

const passportPath = import.meta.env.VITE_PROJECT_PASSPORT_PATH?.trim();
const runId = import.meta.env.VITE_PROJECT_RUN_ID?.trim();
const passport = passportPath && runId && /^[\w/.-]+\.pdf$/.test(passportPath)
  ? { path: passportPath, runId, generatedAtHeader: import.meta.env.VITE_PROJECT_PASSPORT_GENERATED_AT_HEADER?.trim() }
  : undefined;

export const provider = import.meta.env.VITE_DATA_SOURCE === 'api'
  ? createApiProvider({ base: import.meta.env.VITE_API_BASE || '/api', csvBase: import.meta.env.VITE_CSV_ARTIFACT_BASE, passport })
  : createMockProvider({ base: `${import.meta.env.BASE_URL}demo` });
