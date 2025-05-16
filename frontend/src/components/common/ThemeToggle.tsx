import React, { useEffect, useState } from 'react';
import { FontIcon } from '@fluentui/react';
import styles from './ThemeToggle.module.css';

declare global {
  interface Window {
    toggleTheme?: () => void;
  }
}

export const ThemeToggle: React.FC = () => {
  const [isDarkMode, setIsDarkMode] = useState(false);

  useEffect(() => {
    // Initialize state based on current theme
    setIsDarkMode(document.documentElement.classList.contains('dark-mode'));

    // Add event listener for theme changes
    const handleThemeChange = () => {
      setIsDarkMode(document.documentElement.classList.contains('dark-mode'));
    };

    window.addEventListener('themeChanged', handleThemeChange);
    return () => window.removeEventListener('themeChanged', handleThemeChange);
  }, []);

  const handleToggle = () => {
    if (window.toggleTheme) {
      window.toggleTheme();
      // Dispatch custom event to notify theme has changed
      window.dispatchEvent(new Event('themeChanged'));
    }
  };

  return (
    <button 
      className={styles.themeToggle} 
      onClick={handleToggle}
      aria-label={isDarkMode ? 'Switch to light mode' : 'Switch to dark mode'}
      title={isDarkMode ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      <FontIcon iconName={isDarkMode ? 'Sunny' : 'ClearNight'} className={styles.themeIcon} />
    </button>
  );
};

export default ThemeToggle; 