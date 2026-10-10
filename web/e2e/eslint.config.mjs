import config from '../eslint.config.js';
import globals from 'globals';

export default [
  ...config,
  { languageOptions: { globals: { ...globals.browser, ...globals.node } } },
  { files: ['**/*.cjs'], rules: { '@typescript-eslint/no-require-imports': 'off' } },
];
