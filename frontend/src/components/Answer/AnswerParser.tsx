import { cloneDeep } from 'lodash'

import { AskResponse, Citation } from '../../api'

export type ParsedAnswer = {
  citations: Citation[]
  markdownFormatText: string,
  generated_chart: string | null
} | null

export const enumerateCitations = (citations: Citation[]) => {
  const filepathMap = new Map()
  for (const citation of citations) {
    const { filepath } = citation
    let part_i = 1
    if (filepathMap.has(filepath)) {
      part_i = filepathMap.get(filepath) + 1
    }
    filepathMap.set(filepath, part_i)
    citation.part_index = part_i
  }
  return citations
}

/**
 * Extracts a focused context around a specific text in a larger content
 * @param content Full content text
 * @param textToHighlight The text to find and highlight
 * @returns An object with the extracted context and highlight positions
 */
export function extractHighlightContext(content: string, textToHighlight: string) {
  if (!content || !textToHighlight) {
    return { 
      contextText: content, 
      highlightText: textToHighlight
    }
  }

  // Normalize whitespace in both strings for better matching
  const normalizedContent = content.replace(/\s+/g, ' ').trim();
  
  // If textToHighlight is too long (likely the entire content), extract a smaller portion
  // This is an important fix - we don't want to highlight the entire content
  let normalizedHighlight = textToHighlight.replace(/\s+/g, ' ').trim();
  if (normalizedHighlight.length > 200) {
    // Extract key sentences if the highlight is too long (likely the whole document)
    const sentences = normalizedContent.match(/[^.!?]+[.!?]+/g) || [];
    if (sentences.length > 2) {
      // Use middle sentences as they're often most relevant
      const middleIndex = Math.floor(sentences.length / 2);
      const relevantSentences = sentences.slice(
        Math.max(0, middleIndex - 1), 
        Math.min(sentences.length, middleIndex + 2)
      ).join(' ');
      normalizedHighlight = relevantSentences;
    } else {
      // If there aren't enough sentences, just take a portion from the middle
      const startPos = Math.floor(normalizedContent.length / 2) - 100;
      const endPos = startPos + 200;
      normalizedHighlight = normalizedContent.substring(
        Math.max(0, startPos),
        Math.min(normalizedContent.length, endPos)
      );
    }
  }
  
  // Find the position of the highlight text in the content
  const highlightPos = normalizedContent.toLowerCase().indexOf(normalizedHighlight.toLowerCase());
  
  if (highlightPos === -1) {
    // If exact match not found, try to find a partial match (first 50 chars)
    const partialHighlight = normalizedHighlight.substring(0, Math.min(50, normalizedHighlight.length));
    const partialPos = normalizedContent.toLowerCase().indexOf(partialHighlight.toLowerCase());
    
    if (partialPos === -1) {
      // If still no match, find a key sentence or phrase
      const sentences = normalizedContent.match(/[^.!?]+[.!?]+/g) || [];
      if (sentences.length > 0) {
        // Just use the first sentence
        return {
          contextText: normalizedContent,
          highlightText: sentences[0]
        };
      }
      
      // If there are no sentences, just return a segment
      return { 
        contextText: normalizedContent,
        highlightText: normalizedContent.substring(0, Math.min(150, normalizedContent.length))
      };
    }
    
    // Extract the context around the partial match
    const startPos = Math.max(0, partialPos - 100);
    const endPos = Math.min(normalizedContent.length, partialPos + partialHighlight.length + 100);
    return {
      contextText: normalizedContent,
      highlightText: normalizedContent.substring(partialPos, partialPos + Math.min(150, normalizedContent.length - partialPos))
    };
  }
  
  // Extract a window of text around the highlight
  const startHighlight = Math.max(0, highlightPos);
  const endHighlight = Math.min(normalizedContent.length, highlightPos + normalizedHighlight.length);
  
  // Make sure we're not highlighting the entire content
  if (endHighlight - startHighlight > normalizedContent.length * 0.8) {
    // If highlight is more than 80% of content, just highlight a key part
    const middlePoint = Math.floor(normalizedContent.length / 2);
    return {
      contextText: normalizedContent,
      highlightText: normalizedContent.substring(
        Math.max(0, middlePoint - 75),
        Math.min(normalizedContent.length, middlePoint + 75)
      )
    };
  }
  
  return {
    contextText: normalizedContent,
    highlightText: normalizedContent.substring(startHighlight, endHighlight)
  };
}

export function parseAnswer(answer: AskResponse): ParsedAnswer {
  if (typeof answer.answer !== "string") return null
  let answerText = answer.answer
  const citationLinks = answerText.match(/\[(doc\d\d?\d?)]/g)

  const lengthDocN = '[doc'.length

  let filteredCitations = [] as Citation[]
  let citationReindex = 0
  citationLinks?.forEach(link => {
    // Replacing the links/citations with number
    const citationIndex = link.slice(lengthDocN, link.length - 1)
    const citation = cloneDeep(answer.citations[Number(citationIndex) - 1]) as Citation
    if (!filteredCitations.find(c => c.id === citationIndex) && citation) {
      // Extract the text around the citation for better highlighting context
      const linkPosition = answerText.indexOf(link)
      const textBeforeLink = answerText.substring(Math.max(0, linkPosition - 100), linkPosition).trim()
      const textAfterLink = answerText.substring(linkPosition + link.length, Math.min(answerText.length, linkPosition + link.length + 100)).trim()
      
      // Process content for better highlighting
      if (citation.content) {
        // If the citation doesn't have full_content, set it properly
        if (!citation.full_content) {
          citation.full_content = citation.content
        }
        
        // Check if the citation content is too long (likely the entire document)
        // Important: We need to extract only a relevant portion to highlight
        if (citation.content.length > 300) {
          // Try to use context from answer to find relevant citation text
          // Look for some words from before/after the citation in the content
          let relevantContent = citation.content
          
          // Extract key phrases from the text around the citation
          const beforeWords = textBeforeLink.split(/\s+/).slice(-5).join(' ');
          const afterWords = textAfterLink.split(/\s+/).slice(0, 5).join(' ');
          
          // Try to find these phrases in the citation content
          let foundContext = false;
          if (beforeWords && citation.content.includes(beforeWords)) {
            const contextStart = citation.content.indexOf(beforeWords);
            const contextEnd = Math.min(contextStart + 200, citation.content.length);
            relevantContent = citation.content.substring(contextStart, contextEnd);
            foundContext = true;
          } else if (afterWords && citation.content.includes(afterWords)) {
            const contextEnd = citation.content.indexOf(afterWords) + afterWords.length;
            const contextStart = Math.max(0, contextEnd - 200);
            relevantContent = citation.content.substring(contextStart, contextEnd);
            foundContext = true;
          }
          
          // If we couldn't find context, use the extraction function
          if (!foundContext) {
            const { highlightText } = extractHighlightContext(citation.content, citation.content);
            relevantContent = highlightText || citation.content.substring(0, Math.min(200, citation.content.length));
          }
          
          // Set the highlight text to the extracted relevant content
          citation.highlight_text = relevantContent;
        } else {
          // For shorter content, we can just use the whole thing
          citation.highlight_text = citation.content;
        }
        
        // Store context information for better display
        citation.context_before = textBeforeLink
        citation.context_after = textAfterLink
      }
      
      // Replace the citation link with superscript
      answerText = answerText.replaceAll(link, ` ^${++citationReindex}^ `)
      citation.id = citationIndex // original doc index to de-dupe
      citation.reindex_id = citationReindex.toString() // reindex from 1 for display
      filteredCitations.push(citation)
    }
  })

  filteredCitations = enumerateCitations(filteredCitations)

  return {
    citations: filteredCitations,
    markdownFormatText: answerText,
    generated_chart: answer.generated_chart
  }
}
