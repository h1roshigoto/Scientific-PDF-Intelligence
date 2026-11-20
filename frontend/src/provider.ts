import { createApiProvider } from './data';
import { createMockProvider } from './mock';

export const provider = import.meta.env.VITE_DATA_SOURCE === 'api'
  ? createApiProvider({ base: import.meta.env.VITE_API_BASE || '/api', csvBase: import.meta.env.VITE_CSV_ARTIFACT_BASE })
  : createMockProvider({ base: `${import.meta.env.BASE_URL}demo` });
