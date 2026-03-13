DETAILS_QUERY = """
  query JobPubDetailsQuery($id: ID!) {
    jobPubDetails(id: $id) {
      opening {
        status
        publishTime
        contractorTier
        description
        budget { amount currencyCode }
        engagementDuration { label weeks }
        extendedBudgetInfo { hourlyBudgetMin hourlyBudgetMax hourlyBudgetType }
        clientActivity { totalApplicants totalHired numberOfPositionsToHire }
        info { ciphertext id type title createdOn }
        sandsData {
          ontologySkills { id prefLabel }
          additionalSkills { id prefLabel }
        }
      }
      buyer {
        location { city country }
        stats {
          totalAssignments
          hoursCount
          feedbackCount
          score
          totalJobsWithHires
          totalCharges { amount }
        }
        company {
          isEDCReplicated
          contractDate
          profile { industry size }
        }
        jobs { openCount }
      }
    }
  }
"""

# Lightweight query — only fetches ciphertexts for DB dedup check.
SEARCH_IDS_QUERY = """
  query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {
    search {
      universalSearchNuxt {
        visitorJobSearchV1(request: $requestVariables) {
          paging { total offset count }
          results {
            jobTile {
              job {
                ciphertext: cipherText
              }
            }
          }
        }
      }
    }
  }
"""

# # Kept for reference — no longer used in the main scrape flow.
# SEARCH_QUERY = """
#   query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {
#     search {
#       universalSearchNuxt {
#         visitorJobSearchV1(request: $requestVariables) {
#           paging { total offset count }
#           results {
#             id
#             title
#             description
#             ontologySkills { uid prefLabel freeText highlighted }
#             jobTile {
#               job {
#                 id
#                 ciphertext: cipherText
#                 jobType
#                 hourlyBudgetMin
#                 hourlyBudgetMax
#                 contractorTier
#                 createTime
#                 publishTime
#                 fixedPriceAmount { isoCurrencyCode amount }
#               }
#             }
#           }
#         }
#       }
#     }
#   }
# """