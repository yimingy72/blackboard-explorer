import ReactMarkdown from 'react-markdown';
import { readableProse } from '../components/readable';
import styles from './CtfWorkbench.module.css';

export default function Markdown({ text }: { text: string }) {
  return <div className={styles.prose}><ReactMarkdown skipHtml remarkPlugins={[readableProse]} components={{
    h1: ({ children }) => <h3>{children}</h3>, h2: ({ children }) => <h3>{children}</h3>,
    img: ({ alt }) => <span>{alt || '图片未自动加载'}</span>,
    pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
    a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
  }}>{text}</ReactMarkdown></div>;
}
