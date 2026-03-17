SEARCH_IDS_QUERY = """
  query UserJobSearch($requestVariables: UserJobSearchV1Request!) {
    search {
      universalSearchNuxt {
        userJobSearchV1(request: $requestVariables) {
          paging {
            total
            offset
            count
          }
          results {
            id
            jobTile {
              job {
                id
                cipherText
              }
            }
          }
        }
      }
    }
  }
"""

# ── Authenticated job detail query ────────────────────────────────────────────
DETAILS_QUERY = """
  query JobAuthDetailsQuery(
    $id: ID!
    $isLoggedIn: Boolean!
  ) {
    jobAuthDetails(id: $id) {
      opening {
        job {
          status
          publishTime
          postedOn
          contractorTier
          description
          workload
          info {
            ciphertext
            id
            type
            title
            createdOn
          }
          sandsData {
            ontologySkills {
              id
              prefLabel
              groupId
              groupPrefLabel
              relevance
            }
            additionalSkills {
              id
              prefLabel
              relevance
            }
          }
          budget {
            amount
            currencyCode
          }
          extendedBudgetInfo {
            hourlyBudgetMin
            hourlyBudgetMax
            hourlyBudgetType
          }
          engagementDuration {
            label
            weeks
          }
          clientActivity {
            totalApplicants
            totalHired
            numberOfPositionsToHire
          }
          attachments @include(if: $isLoggedIn) {
            fileName
            length
            uri
          }
        }
      }
      buyer {
        isPaymentMethodVerified
        info {
          location {
            city
            country
            countryTimezone
            offsetFromUtcMillis
          }
          stats {
            totalAssignments
            totalJobsWithHires
            hoursCount
            feedbackCount
            score
            totalCharges {
              amount
            }
          }
          company {
            contractDate
          }
          jobs {
            openCount
          }
        }
      }
    }
  }
"""
