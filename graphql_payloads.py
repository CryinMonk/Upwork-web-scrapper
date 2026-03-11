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

SEARCH_QUERY = """
  query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {
    search {
      universalSearchNuxt {
        visitorJobSearchV1(request: $requestVariables) {
          paging { total offset count }
          results {
            id
            title
            description
            ontologySkills { uid prefLabel freeText highlighted }
            jobTile {
              job {
                id
                ciphertext: cipherText
                jobType
                hourlyBudgetMin
                hourlyBudgetMax
                contractorTier
                createTime
                publishTime
                fixedPriceAmount { isoCurrencyCode amount }
              }
            }
          }
        }
      }
    }
  }
"""

# USER_SEARCH_QUERY = """
#   query UserJobSearch($requestVariables: UserJobSearchV1Request!) {
#     search {
#       universalSearchNuxt {
#         userJobSearchV1(request: $requestVariables) {
#           paging {
#             total
#             offset
#             count
#           }
#           facets {
#             jobType { key value }
#             workload { key value }
#             clientHires { key value }
#             durationV3 { key value }
#             amount { key value }
#             contractorTier { key value }
#             contractToHire { key value }
#             paymentVerified: payment { key value }
#             proposals { key value }
#             previousClients { key value }
#           }
#           results {
#             id
#             title
#             description
#             relevanceEncoded
#             ontologySkills {
#               uid
#               parentSkillUid
#               prefLabel
#               prettyName: prefLabel
#               freeText
#               highlighted
#             }
#             isSTSVectorSearchResult
#             applied
#             upworkHistoryData {
#               client {
#                 paymentVerificationStatus
#                 country
#                 totalReviews
#                 totalFeedback
#                 hasFinancialPrivacy
#                 totalSpent {
#                   isoCurrencyCode
#                   amount
#                 }
#               }
#               freelancerClientRelation {
#                 lastContractRid
#                 companyName
#                 lastContractTitle
#               }
#             }
#             jobTile {
#               job {
#                 id
#                 ciphertext: cipherText
#                 jobType
#                 weeklyRetainerBudget
#                 hourlyBudgetMax
#                 hourlyBudgetMin
#                 hourlyEngagementType
#                 contractorTier
#                 sourcingTimestamp
#                 createTime
#                 publishTime
#                 enterpriseJob
#                 personsToHire
#                 premium
#                 totalApplicants
#                 hourlyEngagementDuration {
#                   rid
#                   label
#                   weeks
#                   mtime
#                   ctime
#                 }
#                 fixedPriceAmount {
#                   isoCurrencyCode
#                   amount
#                 }
#                 fixedPriceEngagementDuration {
#                   id
#                   rid
#                   label
#                   weeks
#                   ctime
#                   mtime
#                 }
#               }
#             }
#           }
#         }
#       }
#     }
#   }
# """