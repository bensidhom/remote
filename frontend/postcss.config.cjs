let plugins = {};

try {
  // only include tailwindcss if it's installed
  // this avoids hard failures when node_modules isn't populated
  // (e.g., before running `npm install`)
  require.resolve('tailwindcss');
  plugins.tailwindcss = {};
} catch (e) {
  // tailwind not found — skip it
}

try {
  require.resolve('autoprefixer');
  plugins.autoprefixer = {};
} catch (e) {
  // autoprefixer not found — skip it
}

module.exports = { plugins };
