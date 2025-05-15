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
  const normalizedHighlight = textToHighlight.replace(/\s+/g, ' ').trim();

  // Find the position of the highlight text in the content
  const highlightPos = normalizedContent.toLowerCase().indexOf(normalizedHighlight.toLowerCase());
  
  if (highlightPos === -1) {
    // If exact match not found, try to find a partial match (first 50 chars)
    const partialHighlight = normalizedHighlight.substring(0, Math.min(50, normalizedHighlight.length));
    const partialPos = normalizedContent.toLowerCase().indexOf(partialHighlight.toLowerCase());
    
    if (partialPos === -1) {
      // If still no match, just return the original content and highlight
      return { 
        contextText: content, 
        highlightText: textToHighlight
      }
    }
    
    // Extract the context around the partial match
    const startPos = Math.max(0, partialPos - 100);
    const endPos = Math.min(normalizedContent.length, partialPos + partialHighlight.length + 100);
    return {
      contextText: normalizedContent.substring(startPos, endPos),
      highlightText: normalizedContent.substring(partialPos, partialPos + partialHighlight.length)
    }
  }
  
  // Extract a window of text around the highlight (100 chars before and after)
  const startPos = Math.max(0, highlightPos - 100);
  const endPos = Math.min(normalizedContent.length, highlightPos + normalizedHighlight.length + 100);
  
  return {
    contextText: normalizedContent.substring(startPos, endPos),
    highlightText: normalizedContent.substring(highlightPos, highlightPos + normalizedHighlight.length)
  }
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
        // If the citation doesn't have highlight_text or full_content, set them properly
        if (!citation.full_content) {
          citation.full_content = citation.content
        }
        
        // Extract a more precise highlight context
        const { contextText, highlightText } = extractHighlightContext(citation.content, citation.content)
        
        // Use the extracted highlight text, or fall back to the original content
        citation.highlight_text = highlightText || citation.content
        
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
