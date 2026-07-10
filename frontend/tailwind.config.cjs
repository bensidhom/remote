/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}'
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Inter Variable"', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      colors: {
        // Primary brand ramp (indigo → violet). Used for CTAs, active states, accents.
        brand: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
          950: '#1e1b4b',
        },
        // Accent ramp (cyan/teal) for secondary highlights, charts, "good" states.
        accent: {
          400: '#22d3ee',
          500: '#06b6d4',
          600: '#0891b2',
        },
        // Condition-rating semantic colors (shared by map + charts + badges).
        cond: {
          good: '#22c55e',
          fair: '#f59e0b',
          poor: '#ef4444',
          critical: '#7f1d1d',
          unknown: '#94a3b8',
        },
      },
      borderRadius: {
        '4xl': '2rem',
        '5xl': '2.5rem',
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(99,102,241,0.35), 0 12px 40px -8px rgba(99,102,241,0.45)',
        'glow-accent': '0 0 0 1px rgba(6,182,212,0.35), 0 12px 40px -8px rgba(6,182,212,0.45)',
        card: '0 1px 2px rgba(2,6,23,0.06), 0 12px 32px -12px rgba(2,6,23,0.25)',
        'card-dark': '0 1px 2px rgba(0,0,0,0.4), 0 18px 48px -16px rgba(0,0,0,0.7)',
      },
      backgroundImage: {
        'brand-gradient': 'linear-gradient(90deg,#6366f1,#8b5cf6,#06b6d4)',
        'brand-radial': 'radial-gradient(1200px 600px at 50% -20%, rgba(99,102,241,0.25), transparent 70%)',
      },
      keyframes: {
        shimmer: {
          '0%': { backgroundPosition: '200% center' },
          '100%': { backgroundPosition: '-200% center' },
        },
        floaty: {
          '0%,100%': { transform: 'translateY(0)' },
          '50%': { transform: 'translateY(-6px)' },
        },
        aurora: {
          '0%': { backgroundPosition: '0% 50%' },
          '50%': { backgroundPosition: '100% 50%' },
          '100%': { backgroundPosition: '0% 50%' },
        },
        gradientmove: {
          '0%,100%': { backgroundPosition: '0% 50%' },
          '50%': { backgroundPosition: '100% 50%' },
        },
        'fade-in-up': {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'scale-in': {
          from: { opacity: '0', transform: 'scale(0.97)' },
          to: { opacity: '1', transform: 'scale(1)' },
        },
        pulseglow: {
          '0%,100%': { opacity: '0.6' },
          '50%': { opacity: '1' },
        },
      },
      animation: {
        shimmer: 'shimmer 3s linear infinite',
        floaty: 'floaty 4s ease-in-out infinite',
        aurora: 'aurora 12s ease-in-out infinite',
        gradientmove: 'gradientmove 6s ease infinite',
        'fade-in-up': 'fade-in-up 420ms cubic-bezier(.2,.9,.2,1) both',
        'scale-in': 'scale-in 280ms cubic-bezier(.2,.9,.2,1) both',
        pulseglow: 'pulseglow 2.4s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
