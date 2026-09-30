import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider, theme } from 'antd';
import App from './App';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider theme={{algorithm: theme.darkAlgorithm, token: {
    colorPrimary: '#b5e899', colorBgBase: '#131b17', colorText: '#edf1e9', borderRadius: 10,
    fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif'
  }}}><App /></ConfigProvider></React.StrictMode>
);
