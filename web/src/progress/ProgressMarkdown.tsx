import { Button } from 'antd';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import { readableProse } from '../components/readable';
import styles from './ProgressPage.module.css';

const reference = /^(fact|intent|progress):([A-Za-z0-9_-]{1,100})$/;
export default function ProgressMarkdown({ text, onReference }: { text: string; onReference: (kind: 'fact' | 'intent' | 'progress', id: string) => void }) {
  return <div className={styles.source}><ReactMarkdown skipHtml remarkPlugins={[readableProse]} urlTransform={url => reference.test(url) ? url : defaultUrlTransform(url)} components={{
    h1: ({ children }) => <h3>{children}</h3>, h2: ({ children }) => <h3>{children}</h3>,
    img: ({ alt }) => <span>{alt || '图片未自动加载'}</span>,
    a: ({ href, children }) => {
      const match = href?.match(reference);
      return match ? <Button size="small" type="link" onClick={() => onReference(match[1] as 'fact' | 'intent' | 'progress', match[2])}>{children}</Button> : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>;
    },
  }}>{text}</ReactMarkdown></div>;
}
